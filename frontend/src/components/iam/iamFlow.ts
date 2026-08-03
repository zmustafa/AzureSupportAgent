/**
 * Access Map: turn IAM access facts into a Sankey.
 *
 * Pure — no requests, no clock, no randomness. Everything comes from the facts the server
 * already projected, so the diagram can never disagree with the grid beside it.
 *
 * The columns are configurable for the same reason they are in the Backup flow: "who can do
 * what, where", "who can reach this workload" and "who can touch this resource" are the same
 * data asked three different ways, and hard-coding three screens would triple the surface area
 * for one idea.
 *
 * Two things here are correctness rather than presentation, and both are invisible if you get
 * them wrong:
 *
 * **The group column is not decoration.** Access held through a group cannot be revoked from
 * the person — you remove them from the group. A chain of `principal ▸ role ▸ scope` renders
 * perfectly and tells the operator to do something that will not work.
 *
 * **Eligible is not access.** A PIM-eligible Owner grant is permission to ask. It is excluded
 * by default and its ribbons are drawn as a distinct state when included, because averaging it
 * with standing access overstates privilege.
 */
import type { IamFlowFact } from "../../api";

export type FlowDimension =
  | "principal" | "principal_type" | "group" | "role" | "role_category" | "privileged"
  | "surface" | "access_path" | "state" | "management_group" | "subscription"
  | "resource_group" | "resource_type" | "resource" | "scope_type" | "condition";

/** What a ribbon's width counts. */
export type FlowWeight = "grants" | "principals";

export const DIMENSION_LABELS: Record<FlowDimension, string> = {
  principal: "Principal",
  principal_type: "Principal type",
  group: "Via group",
  role: "Role",
  role_category: "Role category",
  privileged: "Privileged",
  surface: "Surface",
  access_path: "Access path",
  state: "Active / eligible",
  management_group: "Management group",
  subscription: "Subscription",
  resource_group: "Resource group",
  resource_type: "Resource type",
  resource: "Resource",
  scope_type: "Scope level",
  condition: "Has condition",
};

export const WEIGHT_LABELS: Record<FlowWeight, string> = {
  grants: "Grants",
  principals: "Distinct principals",
};

/** Subject ▸ how ▸ verb ▸ object — the question this screen exists to answer. */
export const DEFAULT_CHAIN: FlowDimension[] = ["principal", "group", "role", "subscription"];

/** A chain shorter than this is not a flow, it is a bar chart. */
const MIN_CHAIN = 2;

export type FlowFilters = {
  /** PIM-eligible grants are permission to ask, not standing access. Off by default. */
  includeEligible?: boolean;
  privilegedOnly?: boolean;
  /** Restrict to these surfaces; empty means all. */
  surfaces?: string[];
  principalTypes?: string[];
  /** Free-text match across principal, group, role and scope. */
  query?: string;
};

export type FlowPerspective = {
  chain: FlowDimension[];
  weight: FlowWeight;
  filters: FlowFilters;
};

/**
 * Make a chain from storage safe to render.
 *
 * A stale *filter* simply matches nothing; a stale *column* feeds graph construction, so a
 * dimension renamed or dropped in a later version would produce an empty or malformed chart
 * with no explanation. Unknown and duplicate entries are discarded and a chain left too short
 * falls back to the default rather than rendering something degenerate.
 */
export function sanitizeChain(value: unknown): FlowDimension[] {
  const known = new Set(Object.keys(DIMENSION_LABELS));
  const chain = Array.isArray(value)
    ? value.filter((entry): entry is FlowDimension => typeof entry === "string" && known.has(entry))
    : [];
  const deduped = chain.filter((dimension, index) => chain.indexOf(dimension) === index);
  return deduped.length >= MIN_CHAIN ? deduped : [...DEFAULT_CHAIN];
}

export function sanitizePerspective(value: unknown): FlowPerspective {
  const raw = (value ?? {}) as Partial<FlowPerspective>;
  const filters = (raw.filters ?? {}) as FlowFilters;
  return {
    chain: sanitizeChain(raw.chain),
    weight: raw.weight === "principals" ? "principals" : "grants",
    filters: {
      includeEligible: !!filters.includeEligible,
      privilegedOnly: !!filters.privilegedOnly,
      surfaces: Array.isArray(filters.surfaces) ? filters.surfaces.map(String) : [],
      principalTypes: Array.isArray(filters.principalTypes) ? filters.principalTypes.map(String) : [],
      query: typeof filters.query === "string" ? filters.query : "",
    },
  };
}

const NO_FILTERS: FlowFilters = {
  includeEligible: false, privilegedOnly: false, surfaces: [], principalTypes: [], query: "",
};

/**
 * Starter perspectives, so the feature is useful before anyone has saved anything.
 *
 * Each asks a different question of the same data, which is the point of making the columns
 * configurable at all.
 */
export const FLOW_PRESETS: { name: string; hint: string; perspective: FlowPerspective }[] = [
  {
    name: "Who can do what, where",
    hint: "principal ▸ via group ▸ role ▸ subscription",
    perspective: { chain: ["principal", "group", "role", "subscription"], weight: "grants", filters: { ...NO_FILTERS } },
  },
  {
    name: "Who can reach this workload",
    hint: "Pick a workload above, then read right to left",
    perspective: { chain: ["resource_type", "role", "access_path", "principal"], weight: "principals", filters: { ...NO_FILTERS } },
  },
  {
    name: "Who can touch this resource",
    hint: "Pick a scope above",
    perspective: { chain: ["resource", "role", "access_path", "principal"], weight: "principals", filters: { ...NO_FILTERS } },
  },
  {
    name: "Standing privilege",
    hint: "Privileged roles that nobody has to ask for",
    perspective: { chain: ["principal", "role", "subscription"], weight: "grants", filters: { ...NO_FILTERS, privilegedOnly: true } },
  },
  {
    name: "Privilege you can request",
    hint: "PIM-eligible only — permission to ask, not standing access",
    perspective: { chain: ["principal", "role", "state", "subscription"], weight: "grants", filters: { ...NO_FILTERS, includeEligible: true, privilegedOnly: true } },
  },
  {
    name: "Shadow access",
    hint: "Key Vault policies, classic admins and delegation",
    perspective: { chain: ["surface", "role", "principal_type", "principal"], weight: "grants", filters: { ...NO_FILTERS } },
  },
  {
    name: "Blast radius of a group",
    hint: "What each group actually confers",
    perspective: { chain: ["group", "role", "subscription", "scope_type"], weight: "principals", filters: { ...NO_FILTERS } },
  },
  {
    name: "Inherited vs direct",
    hint: "Where access is really granted",
    perspective: { chain: ["scope_type", "role", "access_path", "principal"], weight: "grants", filters: { ...NO_FILTERS } },
  },
];

export const NODE_COLORS: Record<string, string> = {
  principal: "#2563eb",
  principal_type: "#3b82f6",
  group: "#7c3aed",
  role: "#f59e0b",
  role_category: "#d97706",
  privileged: "#dc2626",
  surface: "#0891b2",
  access_path: "#6366f1",
  state: "#0d9488",
  management_group: "#475569",
  subscription: "#16a34a",
  resource_group: "#64748b",
  resource_type: "#0ea5e9",
  resource: "#0284c7",
  scope_type: "#475569",
  condition: "#a855f7",
};

/** Dimensions whose value is a yes/no rather than a name. */
const BOOLEAN_DIMENSIONS: ReadonlySet<FlowDimension> = new Set(["privileged", "condition"]);

/** Placeholder shown when a fact has no value for a column, so the ribbon never dead-ends. */
const ABSENT: Record<string, string> = {
  group: "Direct (no group)",
  management_group: "No management group",
  subscription: "Not subscription-scoped",
  resource_group: "No resource group",
  resource_type: "No resource type",
  resource: "Whole scope",
};

function valueFor(fact: IamFlowFact, dimension: FlowDimension): { id: string; name: string } | null {
  if (BOOLEAN_DIMENSIONS.has(dimension)) {
    const on = !!fact[dimension];
    const name = dimension === "privileged"
      ? (on ? "Privileged" : "Not privileged")
      : (on ? "Conditional (ABAC)" : "Unconditional");
    return { id: `${dimension}:${on}`, name };
  }
  const raw = String(fact[dimension] ?? "").trim();
  if (!raw) {
    const fallback = ABSENT[dimension];
    // A column the fact genuinely has no value for is skipped, so the ribbon joins the columns
    // either side rather than terminating in a node that means nothing.
    return fallback ? { id: `${dimension}:absent`, name: fallback } : null;
  }
  // Principals and resources are keyed by id where one exists: two people can share a display
  // name, and merging them into one bar would understate how many principals hold the access.
  if (dimension === "principal" && fact.principal_id) return { id: `principal:${fact.principal_id}`, name: raw };
  if (dimension === "group" && fact.group_id) return { id: `group:${fact.group_id}`, name: raw };
  if (dimension === "subscription" && fact.subscription_id) {
    return { id: `subscription:${fact.subscription_id}`, name: raw };
  }
  return { id: `${dimension}:${raw}`, name: raw };
}

export function matchesFilters(fact: IamFlowFact, filters: FlowFilters): boolean {
  if (!filters.includeEligible && fact.state === "Eligible") return false;
  if (filters.privilegedOnly && !fact.privileged) return false;
  if (filters.surfaces?.length && !filters.surfaces.includes(fact.surface)) return false;
  if (filters.principalTypes?.length && !filters.principalTypes.includes(fact.principal_type)) return false;
  const query = (filters.query ?? "").trim().toLowerCase();
  if (query) {
    const haystack = [
      fact.principal, fact.group, fact.role, fact.subscription,
      fact.resource_group, fact.resource, fact.resource_type, fact.surface,
    ].join(" ").toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  return true;
}

/**
 * How many distinct values a single column may draw before the rest are collapsed.
 *
 * Without this the default view of a real tenant put 1,148 principal bars in one column and
 * rendered as a solid block — the route budget then truncated it to 250 of 2,016 links, which
 * is honest and still unreadable. Collapsing the long tail into one labelled "N others" bar
 * keeps the shape of the answer *and* the total, and the operator narrows the focus to open it
 * up. A cap that silently dropped the tail would be the unacceptable version of this.
 */
export const DEFAULT_MAX_PER_COLUMN = 12;

/** Node id for a collapsed remainder within one column. */
const OTHERS = "__others__";

/**
 * Ribbon severity, worst wins.
 *
 * A Sankey cannot draw two ribbons between the same pair of bars — they would occupy the same
 * space, and recharts keys links by their endpoint indices, so a second one is a duplicate React
 * key and gets dropped silently. Links are therefore merged per source→target and carry the
 * most serious state flowing through them, so a path that includes privileged access still
 * reads as privileged rather than being averaged away by the ordinary grants beside it.
 */
const STATUS_RANK: Record<string, number> = { ok: 0, warning: 1, error: 2 };

function worstStatus(a: string, b: string): string {
  return (STATUS_RANK[b] ?? 0) > (STATUS_RANK[a] ?? 0) ? b : a;
}

export type AccessFlowGraph = {
  nodes: { id: string; name: string; kind: string; status?: string; meta?: Record<string, unknown> }[];
  links: { source: string; target: string; value: number; status?: string }[];
  totals: { facts: number; grants: number; principals: number; eligible: number; privileged: number };
  /** Columns whose long tail was folded into a single "N more" bar, and how many that hides. */
  collapsedColumns: { dimension: FlowDimension; hidden: number }[];
};

/**
 * Build the Sankey graph for a chain of dimensions.
 *
 * Node ids are prefixed by their column so the same value appearing in two columns cannot
 * collapse into one node and create a false loop — a subscription used as both scope and label,
 * say. This mirrors the Backup flow deliberately: two diagrams that behave differently for the
 * same reason are two things to learn instead of one.
 */
export function buildAccessFlowGraph(
  facts: IamFlowFact[],
  {
    chain = DEFAULT_CHAIN,
    weight = "grants",
    filters = {},
    maxPerColumn = DEFAULT_MAX_PER_COLUMN,
  }: {
    chain?: FlowDimension[]; weight?: FlowWeight; filters?: FlowFilters; maxPerColumn?: number;
  } = {},
): AccessFlowGraph {
  const columns = chain.filter((dimension, index) => chain.indexOf(dimension) === index);
  const kept = facts.filter((fact) => matchesFilters(fact, filters));

  // Pass one: how big is each value in each column? Needed before any node is created, because
  // whether a value gets its own bar depends on how it ranks against its siblings.
  const weightByValue = new Map<string, Map<string, number>>();
  for (const fact of kept) {
    for (const dimension of columns) {
      const value = valueFor(fact, dimension);
      if (!value) continue;
      let column = weightByValue.get(dimension);
      if (!column) { column = new Map(); weightByValue.set(dimension, column); }
      column.set(value.id, (column.get(value.id) ?? 0) + fact.count);
    }
  }
  const survivors = new Map<string, Set<string>>();
  const collapsedCount = new Map<string, number>();
  for (const [dimension, column] of weightByValue) {
    const ranked = [...column.entries()].sort((a, b) => b[1] - a[1]);
    survivors.set(dimension, new Set(ranked.slice(0, maxPerColumn).map(([id]) => id)));
    collapsedCount.set(dimension, Math.max(0, ranked.length - maxPerColumn));
  }

  const nodes = new Map<string, AccessFlowGraph["nodes"][number]>();
  const links = new Map<string, { source: string; target: string; value: number; status?: string; principals: Set<string> }>();
  const nodePrincipals = new Map<string, Set<string>>();

  let grants = 0;
  let eligible = 0;
  let privileged = 0;
  const allPrincipals = new Set<string>();

  for (const fact of kept) {
    grants += fact.count;
    if (fact.state === "Eligible") eligible += fact.count;
    if (fact.privileged) privileged += fact.count;
    const principalKey = fact.principal_id || fact.principal;
    allPrincipals.add(principalKey);

    // An eligible grant is drawn as a distinct state, not merged into the standing ribbon.
    const status = fact.state === "Eligible" ? "warning" : fact.privileged ? "error" : "ok";

    const present = columns
      .map((dimension) => {
        const value = valueFor(fact, dimension);
        if (!value) return null;
        if (survivors.get(dimension)?.has(value.id)) return { dimension, value, collapsed: false };
        const n = collapsedCount.get(dimension) ?? 0;
        return {
          dimension,
          value: { id: OTHERS, name: `${n.toLocaleString()} more ${DIMENSION_LABELS[dimension].toLowerCase()}` },
          collapsed: true,
        };
      })
      .filter((entry): entry is { dimension: FlowDimension; value: { id: string; name: string }; collapsed: boolean } => !!entry);

    for (let index = 0; index < present.length; index += 1) {
      const { dimension, value, collapsed } = present[index];
      const nodeId = `${dimension}::${value.id}`;
      if (!nodes.has(nodeId)) {
        nodes.set(nodeId, {
          id: nodeId,
          name: value.name,
          kind: dimension,
          status: collapsed ? "disabled" : (dimension === "privileged" && fact.privileged ? "error" : "ok"),
          meta: {
            dimension,
            collapsed,
            principal_id: !collapsed && dimension === "principal" ? fact.principal_id : undefined,
            group_id: !collapsed && dimension === "group" ? fact.group_id : undefined,
            subscription_id: !collapsed && dimension === "subscription" ? fact.subscription_id : undefined,
            scope: !collapsed && (dimension === "resource" || dimension === "subscription") ? fact.scope : undefined,
            role: !collapsed && dimension === "role" ? fact.role : undefined,
          },
        });
      }
      let set = nodePrincipals.get(nodeId);
      if (!set) { set = new Set(); nodePrincipals.set(nodeId, set); }
      set.add(principalKey);

      if (index === 0) continue;
      const previousEntry = present[index - 1];
      const previous = `${previousEntry.dimension}::${previousEntry.value.id}`;
      // A ribbon leaving or entering a folded bar is drawn neutral, whatever it carries. It is
      // the widest thing on screen by construction — it is the whole long tail — and painting it
      // in the privileged colour made "1,136 principals you cannot see" the loudest object on a
      // page whose job is to show you the ones you can.
      const linkStatus = (collapsed || previousEntry.collapsed) ? "disabled" : status;
      // Keyed by the PAIR only. Including the status here produced a second, parallel link
      // between the same two bars whenever one grant was standing and another eligible, which
      // recharts drops as a duplicate key — so some access silently stopped being drawn.
      const key = `${previous}|${nodeId}`;
      const existing = links.get(key);
      if (existing) {
        existing.value += fact.count;
        existing.principals.add(principalKey);
        existing.status = existing.status === "disabled" || linkStatus === "disabled"
          ? "disabled"
          : worstStatus(existing.status ?? "ok", linkStatus);
      } else {
        links.set(key, {
          source: previous, target: nodeId, value: fact.count, status: linkStatus,
          principals: new Set([principalKey]),
        });
      }
    }
  }

  const collapsedColumns = columns
    .filter((dimension) => (collapsedCount.get(dimension) ?? 0) > 0)
    .map((dimension) => ({ dimension, hidden: collapsedCount.get(dimension) ?? 0 }));

  return {
    nodes: [...nodes.values()],
    links: [...links.values()].map((link) => ({
      source: link.source,
      target: link.target,
      // Distinct-principal weighting answers "how many people flow along this ribbon". It is
      // deliberately NOT a sum: one person crossing two ribbons is one person, so these do not
      // add up across columns the way grant counts do. The UI states which unit is in use.
      value: weight === "principals" ? link.principals.size : link.value,
      status: link.status,
    })),
    totals: { facts: kept.length, grants, principals: allPrincipals.size, eligible, privileged },
    collapsedColumns,
  };
}
