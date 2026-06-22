// Original, simplified monoline glyphs drawn in the Azure visual language (not copied
// Microsoft assets). Rendered as data-URI SVGs and used as Cytoscape node background-images
// so the estate graph reads like the Portal instead of flat coloured shapes.
import type { GraphNode, GraphNodeKind } from "../../api";

const _cache = new Map<string, string>();

function dataUri(svg: string): string {
  // SVGs are ASCII-only, so btoa is safe (and produces a compact, cache-friendly key).
  return "data:image/svg+xml;base64," + btoa(svg);
}

/** Wrap inner SVG in a 24x24 monoline frame stroked with `color`. */
function glyph(color: string, inner: string): string {
  return dataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${color}" ` +
      `stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`,
  );
}

// ----------------------------------------------------------------- graph node kinds
const KIND_GLYPH: Record<GraphNodeKind, { color: string; inner: string }> = {
  tenant_connection: {
    color: "#4f46e5",
    inner: '<circle cx="12" cy="12" r="8.2"/><path d="M3.8 12h16.4M12 3.8c-3 2.6-3 13.8 0 16.4M12 3.8c3 2.6 3 13.8 0 16.4"/>',
  },
  management_group: {
    color: "#0d9488",
    inner: '<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="3" y="14.5" width="6" height="5" rx="1"/><rect x="15" y="14.5" width="6" height="5" rx="1"/><path d="M12 8v3M12 11H6v3.5M12 11h6v3.5"/>',
  },
  subscription: {
    color: "#d97706",
    inner: '<circle cx="8.5" cy="8.5" r="3.4"/><path d="M10.9 10.9 19 19M15.5 16.5l2-2M18 19l1.5-1.5"/>',
  },
  resource_group: {
    color: "#475569",
    inner: '<rect x="3" y="3" width="18" height="18" rx="2" stroke-dasharray="3 2"/><path d="M12 7.5l4 2.3v4.4L12 16.5l-4-2.3V9.8z" fill="#475569" fill-opacity="0.16"/>',
  },
  resource: {
    color: "#0ea5e9",
    inner: '<path d="M12 3.2l7.5 4.3v8.6L12 20.4 4.5 16.1V7.5z"/><path d="M12 3.2v8.6M12 11.8l7.5-4.3M12 11.8 4.5 7.5M12 11.8v8.6" stroke-opacity="0.55"/>',
  },
  workload: {
    color: "#059669",
    inner: '<rect x="4" y="4" width="7" height="7" rx="1.4"/><rect x="13" y="4" width="7" height="7" rx="1.4"/><rect x="4" y="13" width="7" height="7" rx="1.4"/><rect x="13" y="13" width="7" height="7" rx="1.4"/>',
  },
  architecture: {
    color: "#7c3aed",
    inner: '<circle cx="12" cy="12" r="8.4"/><path d="M15.6 8.4l-2.2 5.2-5.2 2.2 2.2-5.2z" fill="#7c3aed" fill-opacity="0.22"/><circle cx="12" cy="12" r="1.1"/>',
  },
  architecture_memory: {
    color: "#c026d3",
    inner: '<path d="M9.2 6.4a3 3 0 0 0-3 3 2.6 2.6 0 0 0 0 5 2.6 2.6 0 0 0 3 2.1V6.4z"/><path d="M14.8 6.4a3 3 0 0 1 3 3 2.6 2.6 0 0 1 0 5 2.6 2.6 0 0 1-3 2.1V6.4z"/><path d="M9.2 6.6a3 3 0 0 1 5.6 0"/>',
  },
  assessment_finding: {
    color: "#dc2626",
    inner: '<path d="M12 3.5l9 16h-18z" fill="#dc2626" fill-opacity="0.14"/><path d="M12 9.5v4.5M12 17h0.01"/>',
  },
  rbac_principal: {
    color: "#9333ea",
    inner: '<circle cx="12" cy="8" r="3.2"/><path d="M5.8 19.2c0-3.4 2.8-6.2 6.2-6.2s6.2 2.8 6.2 6.2"/>',
  },
  cost_bucket: {
    color: "#16a34a",
    inner: '<ellipse cx="12" cy="6.5" rx="6.5" ry="2.6"/><path d="M5.5 6.5v5c0 1.4 2.9 2.6 6.5 2.6s6.5-1.2 6.5-2.6v-5M5.5 11.5v5c0 1.4 2.9 2.6 6.5 2.6s6.5-1.2 6.5-2.6v-5"/>',
  },
  retirement_item: {
    color: "#ea580c",
    inner: '<path d="M7 4h10M7 20h10M8 4c0 4 8 5 8 8s-8 4-8 8M16 4c0 4-8 5-8 8s8 4 8 8" fill="#ea580c" fill-opacity="0.1"/>',
  },
  change_event: {
    color: "#2563eb",
    inner: '<circle cx="12" cy="12" r="3.3"/><path d="M3.5 12h5.2M15.3 12h5.2"/>',
  },
  coverage_gap: {
    color: "#f59e0b",
    inner: '<path d="M12 3.5l8 3v5.8c0 5-3.5 7.7-8 9.2-4.5-1.5-8-4.2-8-9.2V6.5z"/><path d="M12 9v4.5" stroke-dasharray="1.6 1.6"/>',
  },
  identity_finding: {
    color: "#db2777",
    inner: '<circle cx="8.5" cy="9.5" r="3.2"/><path d="M10.8 11.8 16.5 17.5M14.5 15.5l1.6-1.6"/><path d="M18 4v3M18 9.4h0.01"/>',
  },
};

export function kindIconUri(kind: GraphNodeKind): string {
  const key = "k:" + kind;
  const hit = _cache.get(key);
  if (hit) return hit;
  const g = KIND_GLYPH[kind] || KIND_GLYPH.resource;
  const uri = glyph(g.color, g.inner);
  _cache.set(key, uri);
  return uri;
}

// ----------------------------------------------------------------- Azure resource types
// Family hues (original, in the Azure palette spirit).
const C_COMPUTE = "#0078D4";
const C_WEB = "#0078D4";
const C_DATA = "#0063B1";
const C_OSS = "#008272"; // PostgreSQL / MySQL (teal)
const C_STORAGE = "#1490DF";
const C_NET = "#008272";
const C_SEC = "#5C2D91";
const C_AI = "#8661C5";
const C_INT = "#C44A9B";
const C_MON = "#8661C5";

type Glyph = { color: string; inner: string };

// Keyed by lowercased ARM type. Curated to cover the bulk of real estates; the generic
// resource cube is the fallback for anything unmapped.
const ARM: Record<string, Glyph> = {
  "microsoft.web/sites": { color: C_WEB, inner: '<circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c-3 2.6-3 13.4 0 16M12 4c3 2.6 3 13.4 0 16"/>' },
  "microsoft.web/serverfarms": { color: C_WEB, inner: '<path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4M4 16l8 4 8-4"/>' },
  "microsoft.web/staticsites": { color: C_WEB, inner: '<path d="M13 3 5 13.5h6l-1 7.5 8-11h-6z" fill="#0078D4" fill-opacity="0.16"/>' },
  "microsoft.sql/servers": { color: C_DATA, inner: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>' },
  "microsoft.sql/servers/databases": { color: C_DATA, inner: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>' },
  "microsoft.sql/managedinstances": { color: C_DATA, inner: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>' },
  "microsoft.dbforpostgresql/flexibleservers": { color: C_OSS, inner: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>' },
  "microsoft.dbformysql/flexibleservers": { color: C_OSS, inner: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>' },
  "microsoft.documentdb/databaseaccounts": { color: C_DATA, inner: '<circle cx="12" cy="12" r="2.2"/><ellipse cx="12" cy="12" rx="9" ry="3.6"/><ellipse cx="12" cy="12" rx="9" ry="3.6" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="9" ry="3.6" transform="rotate(120 12 12)"/>' },
  "microsoft.storage/storageaccounts": { color: C_STORAGE, inner: '<rect x="3" y="6" width="18" height="4.2" rx="1"/><rect x="3" y="13.8" width="18" height="4.2" rx="1"/><circle cx="6.6" cy="8.1" r="0.7" fill="#1490DF"/><circle cx="6.6" cy="15.9" r="0.7" fill="#1490DF"/>' },
  "microsoft.keyvault/vaults": { color: C_WEB, inner: '<path d="M12 3l7 2.5v5.5c0 4.6-3 7.2-7 8.7-4-1.5-7-4.1-7-8.7V5.5z"/><circle cx="12" cy="10" r="2"/><path d="M12 12v3"/>' },
  "microsoft.network/virtualnetworks": { color: C_NET, inner: '<circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="6" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M7.6 7.6 11 15.8M16.4 7.6 13 15.8M8.2 6h7.6"/>' },
  "microsoft.network/networksecuritygroups": { color: C_NET, inner: '<path d="M12 3l8 3v6c0 5-3.5 7.6-8 9-4.5-1.4-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.6 4.5-5"/>' },
  "microsoft.network/publicipaddresses": { color: C_NET, inner: '<circle cx="12" cy="10" r="6"/><path d="M6 10h12M12 4v12"/>' },
  "microsoft.network/loadbalancers": { color: C_NET, inner: '<rect x="9" y="2.5" width="6" height="3" rx="0.8"/><rect x="3" y="13" width="6" height="3" rx="0.8"/><rect x="15" y="13" width="6" height="3" rx="0.8"/><path d="M12 5.5v4M12 9.5 6 13M12 9.5l6 3.5"/>' },
  "microsoft.network/applicationgateways": { color: C_NET, inner: '<path d="M4 9l8-4 8 4v9H4z"/><path d="M9 18v-5h6v5"/>' },
  "microsoft.network/azurefirewalls": { color: C_NET, inner: '<rect x="3.5" y="5" width="17" height="14" rx="1"/><path d="M3.5 9.5h17M3.5 14h17M8 5v4.5M13 9.5v4.5M16 14v5M8 14v5"/>' },
  "microsoft.network/privateendpoints": { color: C_SEC, inner: '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0zM12 16v5"/>' },
  "microsoft.network/privatednszones": { color: C_NET, inner: '<circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c-3 2.6-3 13.4 0 16"/><path d="M8 12c0-2 1.5-3 4-3s4 1 4 3"/>' },
  "microsoft.network/dnszones": { color: C_NET, inner: '<circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c-3 2.6-3 13.4 0 16"/>' },
  "microsoft.network/trafficmanagerprofiles": { color: C_NET, inner: '<circle cx="12" cy="12" r="7.5"/><path d="M12 4.5v15M4.5 12h15M6.5 6.5l11 11M17.5 6.5l-11 11" stroke-opacity="0.5"/>' },
  "microsoft.compute/virtualmachines": { color: C_COMPUTE, inner: '<rect x="3" y="4.5" width="18" height="11.5" rx="1.5"/><path d="M8.5 20h7M12 16v4"/>' },
  "microsoft.compute/virtualmachinescalesets": { color: C_COMPUTE, inner: '<rect x="6" y="3" width="14" height="9" rx="1.2"/><path d="M3 7v11h14"/>' },
  "microsoft.compute/disks": { color: C_COMPUTE, inner: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.2"/>' },
  "microsoft.containerservice/managedclusters": { color: C_COMPUTE, inner: '<path d="M12 3l7.5 4.3v8.6L12 20.4 4.5 16.1V7.5z"/><circle cx="12" cy="11.8" r="2"/><path d="M12 5.5v2.2M12 15.8v2.2M6.5 8.5l1.9 1.1M17.5 8.5l-1.9 1.1"/>' },
  "microsoft.containerregistry/registries": { color: C_COMPUTE, inner: '<rect x="4" y="9" width="16" height="9" rx="1"/><path d="M4 9l3-4h10l3 4M9.5 13.5h5"/>' },
  "microsoft.app/containerapps": { color: C_COMPUTE, inner: '<rect x="3" y="7" width="18" height="10" rx="1"/><path d="M8 7v10M13 7v10M3 11.5h18"/>' },
  "microsoft.insights/components": { color: C_MON, inner: '<path d="M3 13.5h4l2-6.5 3 13 2.5-7.5 1.5 3.5H21"/>' },
  "microsoft.operationalinsights/workspaces": { color: C_MON, inner: '<rect x="4" y="4" width="16" height="16" rx="1.5"/><path d="M8 9h8M8 12h8M8 15h5"/>' },
  "microsoft.cognitiveservices/accounts": { color: C_AI, inner: '<path d="M9.2 6.4a3 3 0 0 0-3 3 2.6 2.6 0 0 0 0 5 2.6 2.6 0 0 0 3 2.1V6.4z"/><path d="M14.8 6.4a3 3 0 0 1 3 3 2.6 2.6 0 0 1 0 5 2.6 2.6 0 0 1-3 2.1V6.4z"/><path d="M9.2 6.6a3 3 0 0 1 5.6 0"/>' },
  "microsoft.search/searchservices": { color: C_AI, inner: '<circle cx="10" cy="10" r="6"/><path d="M14.5 14.5 20 20"/>' },
  "microsoft.servicebus/namespaces": { color: C_INT, inner: '<rect x="3" y="8" width="18" height="8" rx="1"/><path d="M7 12h2M11 12h2M15 12h2"/>' },
  "microsoft.eventhub/namespaces": { color: C_INT, inner: '<circle cx="12" cy="12" r="3"/><path d="M3 12h4M17 12h4M8 8c2-2 6-2 8 0M8 16c2 2 6 2 8 0"/>' },
  "microsoft.eventgrid/topics": { color: C_INT, inner: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 3v18M4 7.5l8 4.5 8-4.5" stroke-opacity="0.6"/>' },
  "microsoft.eventgrid/systemtopics": { color: C_INT, inner: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 3v18M4 7.5l8 4.5 8-4.5" stroke-opacity="0.6"/>' },
  "microsoft.logic/workflows": { color: C_INT, inner: '<circle cx="6" cy="6" r="2.4"/><circle cx="18" cy="12" r="2.4"/><circle cx="6" cy="18" r="2.4"/><path d="M8.4 6H14a2 2 0 0 1 2 2v1.6M8.4 18H14a2 2 0 0 0 2-2v-1.6"/>' },
  "microsoft.cache/redis": { color: "#D9302F", inner: '<rect x="4" y="6" width="16" height="12" rx="1.5"/><path d="M11.5 9l-2.2 4.2h3.2l-1.2 3.3"/>' },
  "microsoft.apimanagement/service": { color: C_COMPUTE, inner: '<path d="M8.5 5C6.5 5 6.6 7 6.6 8.4S5.5 12 4.5 12s1.1 0 2.1.5 1.9 2.1 1.9 3.5M15.5 5c2 0 1.9 2 1.9 3.4S18.5 12 19.5 12s-1.1 0-2.1.5-1.9 2.1-1.9 3.5"/>' },
  "microsoft.datafactory/factories": { color: C_COMPUTE, inner: '<circle cx="8" cy="9" r="2.4"/><circle cx="16" cy="9" r="2.4"/><circle cx="12" cy="16" r="2.4"/><path d="M9.6 10.6 11 13.8M14.4 10.6 13 13.8M10 9h4"/>' },
  "microsoft.dashboard/grafana": { color: C_MON, inner: '<path d="M3 14h4l2-6 3 12 2.5-7 1.5 3H21"/>' },
};

// Family-prefix fallback so an unmapped type still gets a sensible family glyph.
function _familyGlyph(armType: string): Glyph | null {
  if (armType.startsWith("microsoft.network/")) return ARM["microsoft.network/virtualnetworks"];
  if (armType.startsWith("microsoft.compute/")) return ARM["microsoft.compute/virtualmachines"];
  if (armType.startsWith("microsoft.web/")) return ARM["microsoft.web/sites"];
  if (armType.startsWith("microsoft.sql/") || armType.startsWith("microsoft.dbfor")) return ARM["microsoft.sql/servers"];
  if (armType.startsWith("microsoft.storage/")) return ARM["microsoft.storage/storageaccounts"];
  if (armType.startsWith("microsoft.containerservice/") || armType.startsWith("microsoft.containerregistry/") || armType.startsWith("microsoft.app/")) return ARM["microsoft.app/containerapps"];
  if (armType.startsWith("microsoft.cognitiveservices/") || armType.startsWith("microsoft.machinelearningservices/") || armType.startsWith("microsoft.search/")) return ARM["microsoft.cognitiveservices/accounts"];
  if (armType.startsWith("microsoft.insights/") || armType.startsWith("microsoft.operationalinsights/")) return ARM["microsoft.insights/components"];
  if (armType.startsWith("microsoft.servicebus/") || armType.startsWith("microsoft.eventhub/") || armType.startsWith("microsoft.eventgrid/") || armType.startsWith("microsoft.logic/")) return ARM["microsoft.servicebus/namespaces"];
  return null;
}

export function resourceIconUri(armType: string | undefined): string {
  const t = (armType || "").trim().toLowerCase();
  const key = "r:" + t;
  const hit = _cache.get(key);
  if (hit) return hit;
  const g = ARM[t] || _familyGlyph(t) || KIND_GLYPH.resource;
  const uri = glyph(g.color, g.inner);
  _cache.set(key, uri);
  return uri;
}

/** Pick the right icon for any node (resource → ARM-type icon, else kind icon). */
export function nodeIconUri(node: GraphNode): string {
  if (node.kind === "resource") return resourceIconUri(node.data?.type);
  return kindIconUri(node.kind);
}
