import type { GraphEdge, GraphNode, GraphNodeKind } from "../../api";

// ----------------------------------------------------------------- visual language
export type KindMeta = { color: string; shape: string; size: number; label: string; glyph: string };

export const KIND_META: Record<GraphNodeKind, KindMeta> = {
  tenant_connection: { color: "#4f46e5", shape: "hexagon", size: 62, label: "Tenant / connection", glyph: "🔗" },
  management_group: { color: "#0d9488", shape: "round-octagon", size: 54, label: "Management group", glyph: "🏛️" },
  subscription: { color: "#d97706", shape: "round-rectangle", size: 46, label: "Subscription", glyph: "📁" },
  resource_group: { color: "#475569", shape: "round-rectangle", size: 34, label: "Resource group", glyph: "🗂️" },
  resource: { color: "#0ea5e9", shape: "ellipse", size: 26, label: "Resource", glyph: "⬡" },
  workload: { color: "#059669", shape: "round-rectangle", size: 52, label: "Workload", glyph: "🧩" },
  architecture: { color: "#7c3aed", shape: "diamond", size: 42, label: "Architecture", glyph: "📐" },
  architecture_memory: { color: "#c026d3", shape: "tag", size: 32, label: "Memory", glyph: "🧠" },
  assessment_finding: { color: "#dc2626", shape: "triangle", size: 28, label: "Finding", glyph: "⚠️" },
  rbac_principal: { color: "#9333ea", shape: "vee", size: 34, label: "Principal", glyph: "👤" },
  cost_bucket: { color: "#16a34a", shape: "barrel", size: 34, label: "Cost", glyph: "💰" },
  retirement_item: { color: "#ea580c", shape: "triangle", size: 30, label: "Retirement", glyph: "📅" },
  change_event: { color: "#2563eb", shape: "rhomboid", size: 26, label: "Change", glyph: "✎" },
  coverage_gap: { color: "#f59e0b", shape: "triangle", size: 28, label: "Coverage gap", glyph: "▽" },
  identity_finding: { color: "#db2777", shape: "triangle", size: 28, label: "Identity finding", glyph: "🔑" },
};

export const RISK_COLOR: Record<string, string> = { ok: "#059669", low: "#65a30d", medium: "#d97706", high: "#dc2626" };

export const DRIFT_COLOR: Record<string, string> = {
  ok: "#059669",
  documented_missing: "#d97706",
  live_uncontrolled: "#dc2626",
};

// Business-capability colours (workload_type → domain hue).
export const CAPABILITY_COLOR: Record<string, string> = {
  web_app: "#2563eb", website: "#2563eb", crm: "#db2777", erp: "#7c3aed",
  data_pipeline: "#0891b2", ai_ml: "#9333ea", networking: "#0d9488",
  storage: "#ca8a04", identity: "#dc2626", integration: "#ea580c", other: "#64748b",
};

export const CRITICALITY_COLOR: Record<string, string> = {
  critical: "#dc2626", high: "#ea580c", medium: "#d97706", low: "#65a30d",
};

export const EDGE_COLOR: Record<string, string> = {
  contains: "#cbd5e1", member_of: "#94a3b8", belongs_to: "#34d399", models: "#a78bfa",
  documents: "#e879f9", has_finding: "#fca5a5", depends_on: "#fb923c", connects_to: "#38bdf8",
  data_flow: "#22d3ee", private_endpoint_to: "#818cf8", vnet_link: "#2dd4bf", subnet_link: "#5eead4",
  monitors: "#a3e635", can_access: "#c084fc", costs: "#4ade80", retiring_in: "#fb923c",
  changed_in: "#60a5fa", has_gap: "#fbbf24",
};

export const ALL_KINDS = Object.keys(KIND_META) as GraphNodeKind[];

export type Lens =
  | "none" | "risk" | "capability" | "criticality" | "change" | "cost" | "ownership" | "waf" | "shared";

export const LENSES: { id: Lens; label: string }[] = [
  { id: "none", label: "No lens" },
  { id: "risk", label: "Risk" },
  { id: "capability", label: "Business capability" },
  { id: "criticality", label: "Criticality" },
  { id: "change", label: "Change recency" },
  { id: "cost", label: "Cost" },
  { id: "ownership", label: "Ownership" },
  { id: "waf", label: "WAF pillar" },
  { id: "shared", label: "Shared services" },
];

const WAF_COLOR: Record<string, string> = {
  security: "#6366f1", reliability: "#06b6d4", cost: "#10b981", operations: "#f59e0b", performance: "#ec4899",
};

/** A lens colour for a node, or "" to keep the kind default. */
export function lensColor(lens: Lens, node: GraphNode): string {
  const d = node.data || {};
  if (lens === "risk" && node.kind === "workload") return RISK_COLOR[d.risk?.level || "ok"] || "";
  if (lens === "capability" && node.kind === "workload") return CAPABILITY_COLOR[(d.workload_type || "other").toLowerCase()] || CAPABILITY_COLOR.other;
  if (lens === "criticality" && node.kind === "workload") return CRITICALITY_COLOR[(d.criticality || "").toLowerCase()] || "";
  if (lens === "change" && (d.overlay?.changed_recently)) return "#2563eb";
  if (lens === "cost" && node.kind === "subscription" && d.overlay?.cost) {
    const c = d.overlay.cost;
    return c > 5000 ? "#dc2626" : c > 1000 ? "#d97706" : "#16a34a";
  }
  if (lens === "ownership" && node.kind === "workload") {
    const owner = ownerOf(node);
    return owner ? colorFromString(owner) : "#94a3b8";
  }
  if (lens === "waf" && node.kind === "assessment_finding") return WAF_COLOR[(d.pillar || "").toLowerCase()] || "";
  if (lens === "shared" && node.kind === "resource" && (d.workloads || []).length > 1) return "#9333ea";
  return "";
}

export function ownerOf(node: GraphNode): string {
  const tags = node.data?.tags;
  if (Array.isArray(tags)) {
    const owner = tags.find((t: string) => /owner|team/i.test(t));
    if (owner) return owner;
  } else if (tags && typeof tags === "object") {
    for (const k of Object.keys(tags)) {
      if (/owner|team/i.test(k)) return String(tags[k]);
    }
  }
  return node.data?.environment || "";
}

function colorFromString(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffffff;
  const hue = h % 360;
  return `hsl(${hue} 60% 50%)`;
}

// Built loosely (Cytoscape stylesheet union types reject literal blobs) — presentation only.
export function buildStylesheet(lens: Lens): any[] {
  const styles: any[] = [
    {
      selector: "node",
      style: {
        label: "data(label)", color: "#0f172a", "font-size": 9, "text-wrap": "ellipsis",
        "text-max-width": "120px", "text-valign": "bottom", "text-margin-y": 3,
        "border-width": 1.5, "border-color": "#ffffff", "background-color": "#94a3b8",
        width: 28, height: 28, "overlay-padding": 6,
      },
    },
  ];
  for (const kind of ALL_KINDS) {
    const m = KIND_META[kind];
    styles.push({ selector: `node[kind = "${kind}"]`, style: { "background-color": m.color, shape: m.shape, width: m.size, height: m.size } });
  }
  // Lens overrides via a per-node data field set by applyLens().
  styles.push({ selector: "node[lensColor]", style: { "background-color": "data(lensColor)" } });
  // Drift visual language.
  styles.push(
    { selector: 'node[drift = "documented_missing"]', style: { "background-color": DRIFT_COLOR.documented_missing, "border-width": 2, "border-color": "#b45309", "border-style": "dashed" } },
    { selector: 'node[drift = "live_uncontrolled"]', style: { "background-color": DRIFT_COLOR.live_uncontrolled, "border-width": 2, "border-color": "#991b1b" } },
  );
  styles.push(
    { selector: "node:selected", style: { "border-width": 3, "border-color": "#1e293b" } },
    { selector: "node.dim", style: { opacity: 0.1 } },
    { selector: "node.highlight", style: { "border-width": 4, "border-color": "#f59e0b" } },
    { selector: "node.path", style: { "border-width": 4, "border-color": "#2563eb", "background-blacken": -0.2 } },
    { selector: "node.blast-direct", style: { "border-width": 4, "border-color": "#dc2626" } },
    { selector: "node.blast-indirect", style: { "border-width": 3, "border-color": "#f59e0b" } },
    { selector: "node.pulse", style: { "border-width": 4, "border-color": "#2563eb" } },
    {
      selector: "edge",
      style: {
        width: 1.4, "line-color": "#cbd5e1", "target-arrow-color": "#cbd5e1",
        "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", opacity: 0.8,
      },
    },
    { selector: "edge.dim", style: { opacity: 0.05 } },
    { selector: "edge.path", style: { width: 3.5, "line-color": "#2563eb", "target-arrow-color": "#2563eb", opacity: 1 } },
    { selector: 'edge[kind = "models"], edge[kind = "documents"]', style: { "line-style": "dashed" } },
    { selector: 'edge[dependency = "1"]', style: { "line-style": "solid", width: 2 } },
  );
  for (const [kind, color] of Object.entries(EDGE_COLOR)) {
    styles.push({ selector: `edge[kind = "${kind}"]`, style: { "line-color": color, "target-arrow-color": color } });
  }
  void lens; // lens colours are applied as node data, not via selector
  return styles;
}

const DEP_KINDS = new Set(["depends_on", "connects_to", "data_flow", "private_endpoint_to", "vnet_link", "subnet_link", "monitors", "identity_dependency"]);

export function toElements(nodes: GraphNode[], edges: GraphEdge[], lens: Lens = "none"): any[] {
  const out: any[] = [];
  for (const n of nodes) {
    const data: Record<string, any> = {
      id: n.id, kind: n.kind, label: n.label,
      expandable: n.expandable, drift: n.data?.drift || "",
    };
    const lc = lensColor(lens, n);
    if (lc) data.lensColor = lc;
    out.push({ group: "nodes", data });
  }
  for (const e of edges) {
    out.push({ group: "edges", data: { id: e.id, source: e.source, target: e.target, kind: e.kind, dependency: DEP_KINDS.has(e.kind) ? "1" : "0" } });
  }
  return out;
}
