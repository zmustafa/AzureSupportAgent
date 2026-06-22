import type { GraphEdge, GraphNode, GraphNodeKind } from "../../api";
import { nodeIconUri } from "./graphIcons";

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
// Nodes render as white "chips" with the kind/Azure-service icon as a background-image and the
// kind (or lens) colour as the ring (border), so the graph reads like the Portal.
const HUB_KINDS = new Set(["tenant_connection", "management_group", "subscription", "workload"]);

export function buildStylesheet(_lens: Lens, dark = false): any[] {
  const labelColor = dark ? "#e2e8f0" : "#0f172a";
  const labelBg = dark ? "#0b1220" : "#ffffff";
  const chipBg = dark ? "#1e293b" : "#ffffff";
  const styles: any[] = [
    {
      selector: "node",
      style: {
        label: "data(label)", color: labelColor, "font-size": 9, "font-weight": 500,
        "text-wrap": "wrap", "text-max-width": "96px",
        "text-valign": "bottom", "text-margin-y": 5,
        "text-background-color": labelBg, "text-background-opacity": dark ? 0.55 : 0.82,
        "text-background-shape": "round-rectangle", "text-background-padding": 2,
        "background-color": chipBg,
        "background-image": "data(iconUri)",
        "background-fit": "contain",
        "background-clip": "none",
        "background-width": "60%", "background-height": "60%",
        "border-width": 2.4, "border-color": "data(ring)",
        shape: "round-rectangle",
        width: 30, height: 30, "overlay-padding": 6,
        "transition-property": "border-width width height underlay-opacity",
        "transition-duration": "120ms",
      },
    },
    // Hub kinds get a bolder, larger label so the structure reads at a glance.
    { selector: "node.hub", style: { "font-size": 11, "font-weight": 700, "text-max-width": "130px" } },
    // The tenant/connection node is the hero root.
    {
      selector: 'node[kind = "tenant_connection"]',
      style: {
        "font-size": 13, "font-weight": 800,
        "underlay-color": "#6366f1", "underlay-opacity": 0.18, "underlay-padding": 14, "underlay-shape": "ellipse",
      },
    },
  ];
  // Per-kind size; resources render as round chips (ellipse), the rest as rounded squares.
  for (const kind of ALL_KINDS) {
    const m = KIND_META[kind];
    const style: Record<string, any> = { width: m.size, height: m.size };
    if (kind === "resource") style.shape = "ellipse";
    styles.push({ selector: `node[kind = "${kind}"]`, style });
  }
  // Risk/criticality HALO (underlay glow) — keeps the icon crisp while signalling severity.
  styles.push({ selector: "node[halo]", style: { "underlay-color": "data(halo)", "underlay-opacity": 0.4, "underlay-padding": 11, "underlay-shape": "ellipse" } });
  // Collapsed super-node (e.g. "62 findings") reads as a stacked chip.
  styles.push({ selector: "node[collapsed]", style: { "border-width": 3, "border-style": "double", "font-weight": 700 } });
  // Drift visual language — recolour the RING (not the fill) so the icon stays readable.
  styles.push(
    { selector: 'node[drift = "documented_missing"]', style: { "border-color": DRIFT_COLOR.documented_missing, "border-width": 3, "border-style": "dashed" } },
    { selector: 'node[drift = "live_uncontrolled"]', style: { "border-color": DRIFT_COLOR.live_uncontrolled, "border-width": 3 } },
  );
  styles.push(
    { selector: "node.hover", style: { "underlay-color": "#64748b", "underlay-opacity": 0.22, "underlay-padding": 7, "underlay-shape": "ellipse", "z-index": 999 } },
    { selector: "node:selected", style: { "border-width": 3.5, "border-color": dark ? "#f1f5f9" : "#1e293b", "underlay-color": "#3b82f6", "underlay-opacity": 0.3, "underlay-padding": 10, "underlay-shape": "ellipse" } },
    { selector: "node.dim", style: { opacity: 0.08 } },
    { selector: "node.highlight", style: { "border-width": 4, "border-color": "#f59e0b", "underlay-color": "#f59e0b", "underlay-opacity": 0.3, "underlay-padding": 9, "underlay-shape": "ellipse" } },
    { selector: "node.path", style: { "border-width": 4, "border-color": "#2563eb", "underlay-color": "#2563eb", "underlay-opacity": 0.3, "underlay-padding": 9, "underlay-shape": "ellipse" } },
    { selector: "node.blast-direct", style: { "border-width": 4, "border-color": "#dc2626", "underlay-color": "#dc2626", "underlay-opacity": 0.3, "underlay-padding": 9, "underlay-shape": "ellipse" } },
    { selector: "node.blast-indirect", style: { "border-width": 3, "border-color": "#f59e0b" } },
    {
      selector: "edge",
      style: {
        width: "data(weight)", "line-color": "#94a3b8", "target-arrow-color": "#94a3b8",
        "target-arrow-shape": "triangle", "arrow-scale": 0.85, "curve-style": "bezier", opacity: 0.62,
      },
    },
    { selector: "edge.dim", style: { opacity: 0.04 } },
    { selector: "edge.path", style: { width: 4, "line-color": "#2563eb", "target-arrow-color": "#2563eb", opacity: 1, "z-index": 900 } },
    { selector: 'edge[kind = "models"], edge[kind = "documents"]', style: { "line-style": "dashed" } },
    { selector: 'edge[dependency = "1"]', style: { "line-style": "solid", width: 2.4, opacity: 0.85 } },
    // Data-flow / dependency edges get animated marching dashes (offset driven from JS).
    { selector: "edge.flow", style: { "line-style": "dashed", "line-dash-pattern": [6, 4] } },
  );
  for (const [kind, color] of Object.entries(EDGE_COLOR)) {
    styles.push({ selector: `edge[kind = "${kind}"]`, style: { "line-color": color, "target-arrow-color": color } });
  }
  return styles;
}

const DEP_KINDS = new Set(["depends_on", "connects_to", "data_flow", "private_endpoint_to", "vnet_link", "subnet_link", "monitors", "identity_dependency"]);
const FLOW_KINDS = new Set(["data_flow", "connects_to", "depends_on"]);

/** The default ring (border) colour for a node: shared-service resources get a violet ring,
 * everything else its kind colour. */
export function defaultRing(node: GraphNode): string {
  if (node.kind === "resource" && (node.data?.workloads || []).length > 1) return "#9333ea";
  return KIND_META[node.kind]?.color || "#94a3b8";
}

/** Risk/criticality halo colour for a node under a given lens (empty = no halo). */
export function haloColor(lens: Lens, node: GraphNode): string {
  if (node.kind !== "workload") return "";
  if (lens === "risk") return RISK_COLOR[node.data?.risk?.level || "ok"] || "";
  if (lens === "criticality") return CRITICALITY_COLOR[(node.data?.criticality || "").toLowerCase()] || "";
  return "";
}

export function toElements(nodes: GraphNode[], edges: GraphEdge[], lens: Lens = "none"): any[] {
  const out: any[] = [];
  for (const n of nodes) {
    const ring = lensColor(lens, n) || defaultRing(n);
    const data: Record<string, any> = {
      id: n.id, kind: n.kind, label: n.label,
      expandable: n.expandable, drift: n.data?.drift || "",
      iconUri: nodeIconUri(n), ring,
    };
    const halo = haloColor(lens, n);
    if (halo) data.halo = halo;
    if (n.data?.collapsed) data.collapsed = "1";
    const classes = HUB_KINDS.has(n.kind) ? "hub" : "";
    out.push({ group: "nodes", data, classes });
  }
  for (const e of edges) {
    const dep = DEP_KINDS.has(e.kind);
    out.push({
      group: "edges",
      data: { id: e.id, source: e.source, target: e.target, kind: e.kind, dependency: dep ? "1" : "0", weight: dep ? 2.2 : 1.5 },
      classes: FLOW_KINDS.has(e.kind) ? "flow" : "",
    });
  }
  return out;
}
