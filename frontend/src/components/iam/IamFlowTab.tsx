import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamFlowFact } from "../../api";
import { formatError } from "../../utils/format";
import { SankeyExplorer, type FlowNode } from "../shared/SankeyExplorer";
import { FilterRail } from "./IamFilterRail";
import { type AccessFilter, useIamConnectionId } from "./IamShared";
import {
  DEFAULT_CHAIN, DEFAULT_MAX_PER_COLUMN, DIMENSION_LABELS, FLOW_PRESETS, NODE_COLORS,
  WEIGHT_LABELS, buildAccessFlowGraph, matchesFilters, sanitizePerspective,
  type FlowDimension, type FlowFilters, type FlowPerspective, type FlowWeight,
} from "./iamFlow";

/**
 * Access Map — subject ▸ how ▸ verb ▸ object, as a flow.
 *
 * The grid answers "list every grant". This answers "how does this person reach that
 * subscription, and what would I have to change to stop them" — the same rows, read as paths
 * instead of as a table.
 *
 * The columns are configurable rather than fixed because "who can do what", "who can reach this
 * workload" and "who can touch this resource" are one question asked from three ends. Building
 * three screens would have tripled the surface area for one idea, and they would have drifted.
 */

const ALL_DIMENSIONS = Object.keys(DIMENSION_LABELS) as FlowDimension[];
const PERSPECTIVE_KEY = "azsup.iam.flowPerspectives.v1";

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

function Chip({ label, value, tone = "text-gray-900", title }: {
  label: string; value: number | string; tone?: string; title?: string;
}) {
  return (
    <div className="h-8 w-max min-w-16 flex-none rounded-lg border bg-white px-2 py-px" title={title || label}>
      <div data-testid={`accessmap-chip-${label}`}
           className={`text-base font-semibold leading-4 tabular-nums ${tone}`}>{value}</div>
      <div className="whitespace-nowrap text-[8px] font-medium uppercase leading-3 tracking-wide text-gray-400">{label}</div>
    </div>
  );
}

function PerspectiveBar({ current, onApply }: {
  current: FlowPerspective; onApply: (perspective: FlowPerspective) => void;
}) {
  const [saved, setSaved] = useState<{ name: string; p: FlowPerspective }[]>(() => loadPerspectives());

  function persist(list: { name: string; p: FlowPerspective }[]) {
    setSaved(list);
    try { localStorage.setItem(PERSPECTIVE_KEY, JSON.stringify(list)); } catch { /* ignore */ }
  }
  function save() {
    const suggested = current.chain.map((dimension) => DIMENSION_LABELS[dimension]).join(" ▸ ");
    const name = window.prompt("Name this view (columns + weighting + filters):", suggested);
    if (!name?.trim()) return;
    persist([...saved.filter((entry) => entry.name !== name.trim()), { name: name.trim(), p: current }]);
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      <button type="button" onClick={save}
        title="Save the current columns, weighting and filters as a named view"
        className="shrink-0 rounded border px-1.5 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">💾 Save view</button>
      {/* One scrolling row rather than two wrapping ones. These are the feature's main
          signpost — hiding them in a menu would cost more than the row they occupy — but two
          rows of them pushed the diagram off the bottom of a laptop screen. */}
      <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
        {FLOW_PRESETS.map((preset) => (
          <button key={preset.name} type="button" onClick={() => onApply(preset.perspective)}
            title={preset.hint}
            className="shrink-0 whitespace-nowrap rounded-full border border-dashed bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:border-brand hover:text-brand">
            {preset.name}
          </button>
        ))}
        {saved.map((entry) => (
          <span key={entry.name} className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-full border bg-white px-1.5 py-0.5 text-[11px]">
            <button type="button" onClick={() => onApply(entry.p)} className="text-gray-700 hover:text-brand">⭐ {entry.name}</button>
            <button type="button" aria-label={`Delete view ${entry.name}`}
              onClick={() => persist(saved.filter((other) => other.name !== entry.name))}
              className="text-gray-300 hover:text-red-600">✕</button>
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * Everything the diagram cannot draw, on one line.
 *
 * These started as four stacked banners — unreadable groups, denies, folded tails, hidden
 * eligibility. Each is worth saying, and together they pushed the chart to 682px down a 900px
 * viewport, so a laptop user arrived at a page of caveats with a sliver of diagram underneath.
 * Disclosure that displaces the thing it describes is not disclosure. The count stays visible
 * and unmissable; the prose is one click away.
 */
function Caveats({ notes, hiddenEligible, collapsed }: {
  notes: string[];
  hiddenEligible: number;
  collapsed: { dimension: FlowDimension; hidden: number }[];
}) {
  const [open, setOpen] = useState(false);
  const items: string[] = [...notes];
  if (hiddenEligible) {
    items.push(
      `${hiddenEligible.toLocaleString()} PIM-eligible grant(s) are hidden. They are permission to ` +
      `request a role, not access anyone currently holds — tick "Include PIM-eligible" to see what ` +
      `could be activated.`,
    );
  }
  if (collapsed.length) {
    items.push(
      `Long tails folded into one bar per column: ` +
      collapsed.map((c) => `${DIMENSION_LABELS[c.dimension]} (+${c.hidden.toLocaleString()})`).join(", ") +
      `. Every grant is still counted; narrow the focus on the left, or raise "Per column", to open them up.`,
    );
  }
  if (!items.length) return null;

  const summary = [
    notes.some((n) => n.includes("could not be read")) && "unreadable groups",
    notes.some((n) => n.toLowerCase().includes("deny")) && "deny assignments",
    hiddenEligible ? "hidden eligibility" : "",
    collapsed.length ? "folded long tails" : "",
  ].filter(Boolean).join(" · ");

  return (
    <div className="rounded border border-amber-200 bg-amber-50 text-xs text-amber-900">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left">
        <span className="font-medium">
          {items.length} thing{items.length === 1 ? "" : "s"} this diagram cannot draw
        </span>
        <span className="truncate text-amber-800/80">{summary}</span>
        <span className="ml-auto shrink-0 text-amber-700">{open ? "Hide" : "Details"}</span>
      </button>
      {open && (
        <ul className="space-y-1 border-t border-amber-200 px-3 py-2">
          {items.map((item) => <li key={item}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

export function IamFlowTab() {
  const connectionId = useIamConnectionId();
  const fullscreenRef = useRef<HTMLDivElement | null>(null);
  const [scopeFilter, setScopeFilter] = useState<AccessFilter | null>(null);
  const [chain, setChain] = useState<FlowDimension[]>(DEFAULT_CHAIN);
  const [weight, setWeight] = useState<FlowWeight>("grants");
  const [filters, setFilters] = useState<FlowFilters>({
    includeEligible: false, privilegedOnly: false, surfaces: [], principalTypes: [], query: "",
  });
  const [selected, setSelected] = useState<(FlowNode & { value: number }) | null>(null);
  const [maxPerColumn, setMaxPerColumn] = useState(DEFAULT_MAX_PER_COLUMN);

  const q = useQuery({
    queryKey: ["iam", "flow", scopeFilter?.scope_id ?? "", scopeFilter?.workload_id ?? "", connectionId ?? ""],
    queryFn: () => api.iamFlow({
      scope_id: scopeFilter?.scope_id,
      subscription_ids: scopeFilter?.subscription_ids,
      workload_id: scopeFilter?.workload_id,
      connection_id: connectionId,
    }),
    staleTime: 5 * 60 * 1000,
  });

  const facts: IamFlowFact[] = useMemo(() => q.data?.facts ?? [], [q.data]);
  const graph = useMemo(
    () => buildAccessFlowGraph(facts, { chain, weight, filters, maxPerColumn }),
    [facts, chain, weight, filters, maxPerColumn],
  );

  const surfaces = useMemo(
    () => [...new Set(facts.map((f) => f.surface))].filter(Boolean).sort(),
    [facts],
  );
  const principalTypes = useMemo(
    () => [...new Set(facts.map((f) => f.principal_type))].filter(Boolean).sort(),
    [facts],
  );
  /** All columns except the last; the final column must label inward or it clips. */
  const labelRightKinds = useMemo(() => new Set(chain.slice(0, -1)), [chain]);
  // The legend is built from the colour map, so passing all sixteen dimensions drew sixteen
  // swatches under a four-column diagram - most of them for columns that are not on screen.
  const colors = useMemo(
    () => Object.fromEntries(chain.map((d) => [d, NODE_COLORS[d]]).filter(([, c]) => !!c)),
    [chain],
  );
  // ...and label it the way the column pickers do. Left to default it rendered the raw keys
  // ("principal type", "resource group"), so the legend and the controls named the same axis
  // two different ways on one screen.
  const legend = useMemo(
    () => chain.filter((d) => NODE_COLORS[d]).map((d) => [DIMENSION_LABELS[d], NODE_COLORS[d]] as [string, string]),
    [chain],
  );

  // A selection outlives the thing it described when a filter or a column change removes that
  // node. The panel then sat there reporting "0 grant(s), 0 principal(s)" about a bar nobody
  // could see, which reads as a broken panel rather than as an emptied one.
  useEffect(() => {
    if (selected && !graph.nodes.some((n) => n.id === selected.id)) setSelected(null);
  }, [graph, selected]);

  const setColumn = (index: number, dimension: FlowDimension | "") => {
    setChain((current) => {
      if (dimension === "") return current.filter((_value, position) => position !== index);
      // Picking a dimension already in the chain SWAPS the two columns. Disabling it instead
      // would make reordering impossible, and allowing a duplicate would draw a self-loop.
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
    setFilters(perspective.filters);
    setSelected(null);
  };

  const toggle = (key: "includeEligible" | "privilegedOnly") =>
    setFilters((current) => ({ ...current, [key]: !current[key] }));

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading access map…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;

  const data = q.data!;
  const eligibleShown = filters.includeEligible;
  const eligibleAvailable = data.totals.eligible_rows;

  return (
    <div className="flex h-full min-h-0">
      {/* Scope and workload focus. "Who can reach this workload" is this rail plus a preset,
          not a separate screen. */}
      <FilterRail
        filter={scopeFilter}
        onChange={setScopeFilter}
        collapsible
        storageKey="azsup.iam.accessMap.filterRail"
      />
      <div ref={fullscreenRef} className="flex min-w-0 flex-1 flex-col overflow-auto bg-gray-50 p-4">
      <div className="shrink-0 space-y-3">
      {/* What the diagram is counting. Ribbon width is meaningless without it. */}
      <div className="flex flex-wrap items-center gap-2">
        <Chip label="Grants" value={graph.totals.grants.toLocaleString()}
              title="Assignment rows currently drawn" />
        <Chip label="Principals" value={graph.totals.principals.toLocaleString()} />
        <Chip label="Privileged" value={graph.totals.privileged.toLocaleString()}
              tone={graph.totals.privileged ? "text-rose-600" : "text-gray-900"} />
        {/* A value slot must hold a number. It previously read "hidden", which looks like a
            rendering fault rather than a deliberate exclusion. */}
        <Chip label="Eligible"
              value={eligibleShown ? graph.totals.eligible.toLocaleString() : `0 / ${eligibleAvailable.toLocaleString()}`}
              tone={eligibleShown ? "text-amber-600" : "text-gray-400"}
              title={eligibleShown
                ? "PIM-eligible grants included in the diagram"
                : `${eligibleAvailable.toLocaleString()} PIM-eligible grant(s) excluded — permission to ask, not standing access`} />
        {!!data.totals.deny_rows && (
          <Chip label="Deny (not drawn)" value={data.totals.deny_rows} tone="text-rose-600"
                title="Deny assignments remove access; a ribbon would add it" />
        )}
      </div>

      <PerspectiveBar current={{ chain, weight, filters }} onApply={applyPerspective} />

      {/* Column pickers, weighting and filters. */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-white p-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Columns</span>
        {chain.map((dimension, index) => (
          <select
            key={`${dimension}-${index}`}
            value={dimension}
            aria-label={`Column ${index + 1}`}
            onChange={(e) => setColumn(index, e.target.value as FlowDimension | "")}
            className="rounded border px-1.5 py-0.5 text-[12px]"
          >
            {ALL_DIMENSIONS.map((option) => (
              <option key={option} value={option}>{DIMENSION_LABELS[option]}</option>
            ))}
            {chain.length > 2 && <option value="">— remove —</option>}
          </select>
        ))}
        {chain.length < 6 && (
          <button type="button"
            onClick={() => setChain((c) => [...c, ALL_DIMENSIONS.find((d) => !c.includes(d)) ?? "role"])}
            className="rounded border px-1.5 py-0.5 text-[12px] text-gray-600 hover:bg-gray-50">+ column</button>
        )}

        <span className="ml-3 text-[11px] font-medium uppercase tracking-wide text-gray-400">Width</span>
        <select value={weight} aria-label="Ribbon width"
          onChange={(e) => setWeight(e.target.value as FlowWeight)}
          className="rounded border px-1.5 py-0.5 text-[12px]">
          {(Object.keys(WEIGHT_LABELS) as FlowWeight[]).map((option) => (
            <option key={option} value={option}>{WEIGHT_LABELS[option]}</option>
          ))}
        </select>

        <span className="ml-3 text-[11px] font-medium uppercase tracking-wide text-gray-400">Per column</span>
        <select value={maxPerColumn} aria-label="Values per column"
          onChange={(e) => setMaxPerColumn(Number(e.target.value))}
          className="rounded border px-1.5 py-0.5 text-[12px]">
          {[8, 12, 20, 40, 100].map((option) => (
            <option key={option} value={option}>Top {option}</option>
          ))}
        </select>

        <label className="ml-3 inline-flex items-center gap-1 text-[12px] text-gray-700"
               title="PIM-eligible grants are permission to ask for a role, not access someone currently holds">
          <input type="checkbox" checked={!!filters.includeEligible} onChange={() => toggle("includeEligible")} />
          Include PIM-eligible
          {!!eligibleAvailable && <span className="text-gray-400">({eligibleAvailable})</span>}
        </label>
        <label className="inline-flex items-center gap-1 text-[12px] text-gray-700">
          <input type="checkbox" checked={!!filters.privilegedOnly} onChange={() => toggle("privilegedOnly")} />
          Privileged roles only
        </label>

        <select value={(filters.surfaces ?? [])[0] ?? ""} aria-label="Surface"
          onChange={(e) => setFilters((c) => ({ ...c, surfaces: e.target.value ? [e.target.value] : [] }))}
          className="rounded border px-1.5 py-0.5 text-[12px]">
          <option value="">All surfaces</option>
          {surfaces.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={(filters.principalTypes ?? [])[0] ?? ""} aria-label="Principal type"
          onChange={(e) => setFilters((c) => ({ ...c, principalTypes: e.target.value ? [e.target.value] : [] }))}
          className="rounded border px-1.5 py-0.5 text-[12px]">
          <option value="">All principal types</option>
          {principalTypes.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input value={filters.query ?? ""} aria-label="Filter the data"
          onChange={(e) => setFilters((c) => ({ ...c, query: e.target.value }))}
          placeholder="Filter to a principal, role or scope…"
          title="Filters the underlying access BEFORE the top-N ranking, so it can find someone who is currently inside a folded bar. The search box on the chart only highlights what is already drawn."
          className="w-64 rounded border px-1.5 py-0.5 text-[12px]" />
      </div>

      {/* Facts the diagram cannot draw. Rendering these as silence would be the lie; rendering
          them as four banners pushed the diagram off the bottom of a laptop screen. */}
      <Caveats
        notes={data.notes}
        hiddenEligible={eligibleShown ? 0 : eligibleAvailable}
        collapsed={graph.collapsedColumns}
      />
      </div>

      <div data-testid="accessmap-sankey-region" className="mt-3 min-h-[430px] flex-1">
        <SankeyExplorer
          storageKey="azsup.iam.accessMap"
          nodes={graph.nodes}
          links={graph.links}
          heightPx={430}
          fillHeight
          fullscreenTargetRef={fullscreenRef}
          title="Access map"
          subtitle={
            weight === "grants"
              ? "Ribbon width is the number of grants. Click any bar or ribbon to trace its complete paths."
              : "Ribbon width is the number of distinct principals — these do not add up across columns, because one person crossing two ribbons is still one person."
          }
          colors={colors}
          legend={legend}
          // Every column but the last draws its label to the right of the bar. The last one
          // draws inward, because at the right-hand edge there is nothing to draw into: the
          // destination names clipped to "perf-t", "Citrix", "Not su" — the one column whose
          // value the reader is chasing was the one they could not read.
          labelRightKinds={labelRightKinds}
          onSelectNode={(node) => setSelected(node)}
          emptyMessage="No access matches these filters."
          onClearFilters={() => setFilters({
            includeEligible: false, privilegedOnly: false, surfaces: [], principalTypes: [], query: "",
          })}
        />
      </div>

      {selected && (
        <div className="mt-3 shrink-0">
          <SelectionDetail node={selected} facts={facts} filters={filters} onClose={() => setSelected(null)} />
        </div>
      )}
      </div>
    </div>
  );
}

/**
 * What one selected bar actually contains, and where to go to act on it.
 *
 * A Sankey shows shape; it cannot show a name list. Without this the operator can see that
 * eleven people reach a subscription and has no way to find out who.
 */
function SelectionDetail({ node, facts, filters, onClose }: {
  node: FlowNode & { value: number };
  facts: IamFlowFact[];
  filters: FlowFilters;
  onClose: () => void;
}) {
  const dimension = node.meta?.dimension as FlowDimension | undefined;
  const ref = useRef<HTMLDivElement | null>(null);

  // The panel renders below a 480px chart, which on a laptop is off the bottom of the screen.
  // Clicking a bar then dimmed the diagram and appeared to do nothing else, because the thing
  // it produced was somewhere the reader could not see.
  useEffect(() => {
    ref.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [node.id]);

  const matching = useMemo(() => {
    if (!dimension) return [];
    return facts.filter((fact) => {
      if (!matchesFilters(fact, filters)) return false;
      if (dimension === "privileged") return fact.privileged === (node.name === "Privileged");
      if (dimension === "condition") return fact.condition === (node.name === "Conditional (ABAC)");
      return String(fact[dimension] ?? "") === node.name;
    });
  }, [dimension, facts, filters, node.name]);

  const principals = [...new Set(matching.map((f) => f.principal))].sort();
  const roles = [...new Set(matching.map((f) => f.role))].sort();
  const scopes = [...new Set(matching.map((f) => f.subscription || f.scope || "").filter(Boolean))].sort();
  const scope = node.meta?.scope as string | undefined;
  const principalId = node.meta?.principal_id as string | undefined;

  return (
    <div ref={ref} data-testid="accessmap-selection" className="rounded-lg border bg-white p-3 text-[13px]">
      <div className="flex items-start justify-between">
        <div>
          <div className="font-semibold text-gray-900">{node.name}</div>
          <div className="text-xs text-gray-500">
            {DIMENSION_LABELS[dimension ?? "role"]} · {matching.length.toLocaleString()} grant(s) ·{" "}
            {principals.length.toLocaleString()} principal(s)
          </div>
        </div>
        <button type="button" onClick={onClose} aria-label="Close detail"
          className="text-gray-400 hover:text-gray-700">✕</button>
      </div>

      <div className="mt-2 grid gap-3 md:grid-cols-2">
        {/* Do not echo the selected column back at the reader: selecting the Owner role and
            being told the roles involved are "Owner" spends half the panel saying nothing. */}
        {dimension !== "principal" && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">
              Principals ({principals.length.toLocaleString()})
            </div>
            <ul className="mt-1 max-h-40 space-y-0.5 overflow-auto text-gray-700">
              {principals.slice(0, 50).map((p) => <li key={p} className="truncate">{p}</li>)}
            </ul>
            {principals.length > 50 && (
              <div className="text-xs text-gray-400">…and {principals.length - 50} more</div>
            )}
          </div>
        )}
        {dimension !== "role" && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">
              Roles ({roles.length.toLocaleString()})
            </div>
            <ul className="mt-1 max-h-40 space-y-0.5 overflow-auto text-gray-700">
              {roles.slice(0, 50).map((r) => <li key={r} className="truncate">{r}</li>)}
            </ul>
          </div>
        )}
        {/* Selecting a principal makes "which scopes does this reach" the question. */}
        {dimension === "principal" && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">
              Scopes reached ({scopes.length.toLocaleString()})
            </div>
            <ul className="mt-1 max-h-40 space-y-0.5 overflow-auto text-gray-700">
              {scopes.slice(0, 50).map((s) => <li key={s} className="truncate">{s}</li>)}
            </ul>
          </div>
        )}
      </div>

      {/* The picture is a starting point; these are the screens that let you act. */}
      <div className="mt-2 flex flex-wrap gap-2">
        {scope && (
          <a className="rounded border px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50"
             href={`/iam/evaluate?mode=who&scope=${encodeURIComponent(scope)}`}>
            Who can access this scope →
          </a>
        )}
        {principalId && (
          <a className="rounded border px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50"
             href={`/iam/evaluate?mode=can&principal=${encodeURIComponent(principalId)}`}>
            What can this principal do →
          </a>
        )}
        <a className="rounded border px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50"
           href={`/iam/effective?search=${encodeURIComponent(node.name)}`}>
          Open in the access grid →
        </a>
      </div>
    </div>
  );
}
