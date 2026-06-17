import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Handle,
  Position,
  MarkerType,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  useReactFlow,
  useViewport,
  type Node,
  type Edge,
  type Connection,
  type NodeChange,
  type EdgeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  api,
  type Architecture,
  type ArchNode,
  type ArchEdge,
  type ArchEdgeKind,
  type ArchGroup,
  type ArchitectureCatalog,
  type ArchitecturePaletteItem,
  type AssessmentFinding,
} from "../api";
import { AzureIcon, friendlyResourceType, friendlyLocation } from "./AzureIcon";
import { formatError } from "../utils/format";
import { NetCheckModal } from "./NetCheckModal";
import { DnsDebugModal } from "./DnsDebugModal";

const LAYER_ORDER = [
  "edge", "presentation", "application", "integration", "data",
  "networking", "security", "monitoring", "shared",
];
const EDGE_KINDS: { id: ArchEdgeKind; label: string; color: string }[] = [
  { id: "data_flow", label: "Data flow", color: "#2563eb" },
  { id: "connects_to", label: "Connects to", color: "#0d9488" },
  { id: "network", label: "Network", color: "#0891b2" },
  { id: "depends_on", label: "Depends on", color: "#6b7280" },
  { id: "identity", label: "Identity", color: "#b91c1c" },
  { id: "monitors", label: "Monitors", color: "#ca8a04" },
];
const edgeColor = (k: string) => EDGE_KINDS.find((e) => e.id === k)?.color ?? "#6b7280";

// --- Azure semantics helpers (hosting model, reachability, cost, connectors) --------
// Hosting model (shared-responsibility lens). "Net" = network plumbing, "" = concept/note.
function hostingModel(type: string): "IaaS" | "PaaS" | "SaaS" | "Net" | "" {
  const t = (type || "").toLowerCase();
  if (!t || t === "__note__") return "";
  if (/virtualnetworks|subnets|networksecuritygroups|publicipaddresses|networkinterfaces|loadbalancers|natgateways|azurefirewalls|virtualnetworkgateways|privateendpoints|privatednszones|routetables|applicationgateways|frontdoors|trafficmanager|expressroute|bastionhosts|ipgroups/.test(t)) return "Net";
  if (/virtualmachines|virtualmachinescalesets|\/disks|availabilitysets|diskencryptionsets|images\b|snapshots/.test(t)) return "IaaS";
  if (/securityinsights|microsoftdefender|sentinel|purview|microsoft\.security|advisor/.test(t)) return "SaaS";
  return "PaaS"; // managed services default to PaaS
}
const HOSTING_META: Record<string, { label: string; cls: string }> = {
  IaaS: { label: "IaaS", cls: "bg-orange-100 text-orange-700" },
  PaaS: { label: "PaaS", cls: "bg-sky-100 text-sky-700" },
  SaaS: { label: "SaaS", cls: "bg-violet-100 text-violet-700" },
  Net: { label: "Net", cls: "bg-teal-100 text-teal-700" },
};

// Public vs private reachability from real config (meta.publicNetworkAccess, type, PE link).
function isPublicType(t: string) { return /publicipaddresses|applicationgateways|frontdoors|trafficmanager|bastionhosts/.test((t || "").toLowerCase()); }
function isPrivateEndpointType(t: string) { return /privateendpoints/.test((t || "").toLowerCase()); }
// PaaS data/control-plane services whose public exposure is a real risk worth flagging.
function isExposablePaaS(t: string) {
  return /storage\/storageaccounts|sql\/servers|documentdb|keyvault\/vaults|cache\/redis|servicebus\/namespaces|web\/sites|containerregistry|cognitiveservices|search\/searchservices|eventhub/.test((t || "").toLowerCase());
}

// Very rough indicative monthly cost (USD) by type + sku — for visibility, NOT billing.
function estMonthlyCost(type: string, sku: string): number {
  const t = (type || "").toLowerCase(), s = (sku || "").toLowerCase();
  if (/virtualmachinescalesets/.test(t)) return 280;
  if (/virtualmachines/.test(t)) { if (/e\d|m\d/.test(s)) return 350; if (/d.*s?_v|standard_d/.test(s)) return 140; if (/b1|b2|basic/.test(s)) return 40; return 120; }
  if (/\/disks/.test(t)) return /premium/.test(s) ? 20 : 6;
  if (/web\/serverfarms|serverfarm/.test(t)) { if (/p\dv|premium/.test(s)) return 220; if (/s\d|standard/.test(s)) return 75; if (/b\d|basic/.test(s)) return 13; return 55; }
  if (/web\/sites|sites\b/.test(t)) return /y1|consumption/.test(s) ? 0 : 0; // billed via plan / consumption
  if (/sql\/managedinstances/.test(t)) return /gp_gen5_8|generalpurpose/.test(s) ? 1500 : 1100; // SQL MI is pricey
  if (/sql\/servers/.test(t)) return /gp_|generalpurpose/.test(s) ? 380 : 200;
  if (/documentdb|cosmos/.test(t)) return 60;
  if (/storage\/storageaccounts/.test(t)) return 25;
  if (/cache\/redis/.test(t)) return /premium/.test(s) ? 410 : 55;
  if (/azurefirewalls/.test(t)) return 950;
  if (/applicationgateways/.test(t)) return /waf/.test(s) ? 330 : 180;
  if (/frontdoors/.test(t)) return /premium/.test(s) ? 330 : 35;
  if (/virtualnetworkgateways/.test(t)) return /vpngw[2-5]/.test(s) ? 380 : 140;
  if (/bastionhosts/.test(t)) return 140;
  if (/natgateways/.test(t)) return 45;
  if (/loadbalancers/.test(t)) return /standard/.test(s) ? 22 : 0;
  if (/publicipaddresses/.test(t)) return 4;
  if (/managedclusters|kubernetes/.test(t)) return 220;
  if (/containerapps|managedenvironments/.test(t)) return 40;
  if (/containerregistry/.test(t)) return /premium/.test(s) ? 50 : /standard/.test(s) ? 20 : 5;
  if (/servicebus/.test(t)) return /premium/.test(s) ? 680 : 10;
  if (/eventhub/.test(t)) return /premium|dedicated/.test(s) ? 700 : 22;
  if (/apimanagement/.test(t)) return /premium/.test(s) ? 2800 : /standard/.test(s) ? 670 : /developer/.test(s) ? 50 : 140;
  if (/logic\/workflows/.test(t)) return 15;
  if (/datafactory/.test(t)) return 80;
  if (/synapse\/workspaces/.test(t)) return /dw\d/.test(s) ? 1080 : 200; // DW200c ~$1.5/hr
  if (/powerbidedicated/.test(t)) return /a1|p1/.test(s) ? 750 : 1500;
  if (/purview/.test(t)) return 400;
  if (/recoveryservices/.test(t)) return 60;
  if (/cognitiveservices|openai/.test(t)) return 100;
  return 0;
}
function fmtUsd(n: number): string { return n >= 1000 ? `$${(n / 1000).toFixed(1)}k` : `$${Math.round(n)}`; }

const isVNet = (t: string) => /virtualnetworks/.test((t || "").toLowerCase()) && !/subnets/.test((t || "").toLowerCase());
const isGatewayType = (t: string) => /virtualnetworkgateways|expressroutecircuits|connections\b/.test((t || "").toLowerCase());

// --- model <-> React Flow conversions --------------------------------------
type AzData = ArchNode & { color: string; [key: string]: unknown };

function toFlowNodes(arch: Architecture, colorOf: (c: string) => string): Node<AzData>[] {
  return arch.nodes.map((n) => ({
    id: n.id,
    type: "azure",
    position: { x: n.x || 0, y: n.y || 0 },
    data: { ...n, color: colorOf(n.category) },
  }));
}
function toFlowEdges(arch: Architecture): Edge[] {
  return arch.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label || undefined,
    animated: e.kind === "data_flow",
    markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(e.kind) },
    style: { stroke: edgeColor(e.kind), strokeWidth: 1.6, strokeDasharray: e.dashed ? "5 4" : undefined },
    data: { kind: e.kind, dashed: e.dashed },
    labelStyle: { fontSize: 10, fill: "#475569" },
    labelBgStyle: { fill: "#fff", fillOpacity: 0.85 },
  }));
}

// --- custom Azure resource node --------------------------------------------
function AzureNodeCard({ data, selected }: NodeProps) {
  const d = data as unknown as AzData;
  // Sticky-note annotation: a free-form text shape (not an Azure resource).
  if (d.type === "__note__") {
    const dimN = Boolean(d._dim);
    return (
      <div
        className={`relative min-w-[140px] max-w-[260px] rounded-md border px-3 py-2 text-[12px] leading-snug shadow-sm transition ${selected ? "ring-2 ring-amber-500" : ""}`}
        style={{ background: "#fef9c3", borderColor: "#fde047", color: "#713f12", opacity: dimN ? 0.3 : 1, whiteSpace: "pre-wrap" }}
      >
        {["Left", "Right", "Top", "Bottom"].map((side) => {
          const pos = Position[side as keyof typeof Position];
          return (
            <span key={side}>
              <Handle id={`t-${side}`} type="target" position={pos} className="!h-2 !w-2 !border !border-amber-300 !bg-amber-50" />
              <Handle id={`s-${side}`} type="source" position={pos} className="!h-2 !w-2 !border !border-amber-300 !bg-amber-400" />
            </span>
          );
        })}
        <span className="mr-1">📝</span>{d.name || "Note"}
      </div>
    );
  }
  const meta = Object.entries(d.meta || {}).slice(0, 4);
  const dim = Boolean(d._dim);
  const lintCount = Number(d._lintCount) || 0;
  const lintSev = d._lintSev as string;
  const assessSev = d._assessSev as string;
  const assessCount = Number(d._assessCount) || 0;
  const ASSESS_RING: Record<string, string> = { critical: "#dc2626", error: "#ea580c", warning: "#d97706", info: "#0284c7" };
  const ring = assessSev ? ASSESS_RING[assessSev] : "";
  // Azure-semantics overlays (toggled via the "Azure view" button).
  const azureView = Boolean(d._azureView);
  const pillarTint = (d._pillarTint as string) || "";
  const hosting = azureView ? hostingModel(d.type) : "";
  const reach = d._reach as string; // "public" | "private" | ""
  const cost = Number(d._cost) || 0;
  return (
    <div
      className={`relative min-w-[170px] max-w-[230px] rounded-xl border bg-white shadow-sm transition ${selected ? "ring-2 ring-brand" : "border-gray-200"}`}
      style={{ borderTopColor: d.color, borderTopWidth: 3, opacity: dim ? 0.2 : 1, boxShadow: pillarTint ? `0 0 0 2px ${pillarTint}, 0 0 14px ${pillarTint}77` : ring ? `0 0 0 2px ${ring}, 0 0 12px ${ring}66` : reach === "public" && azureView ? "0 0 0 2px #ef444466" : undefined }}
    >
      {assessCount > 0 && (
        <span
          title={`${assessCount} failing assessment control(s) — worst: ${assessSev}`}
          className="absolute -left-1.5 -top-1.5 z-10 flex h-4 items-center justify-center rounded-full px-1 text-[9px] font-bold text-white"
          style={{ background: ring || "#dc2626" }}
        >
          🛡 {assessCount}
        </span>
      )}
      {lintCount > 0 && (
        <span
          title={`${lintCount} best-practice ${lintCount === 1 ? "hint" : "hints"}`}
          className={`absolute -right-1.5 -top-1.5 z-10 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] font-bold text-white ${lintSev === "warning" ? "bg-amber-500" : "bg-sky-500"}`}
        >
          {lintCount}
        </span>
      )}
      {["Left", "Right", "Top", "Bottom"].map((side) => {
        const pos = Position[side as keyof typeof Position];
        return (
          <span key={side}>
            <Handle id={`t-${side}`} type="target" position={pos} className="!h-2 !w-2 !border !border-gray-300 !bg-white" />
            <Handle id={`s-${side}`} type="source" position={pos} className="!h-2 !w-2 !border !border-gray-300 !bg-gray-400" />
          </span>
        );
      })}
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md" style={{ background: `${d.color}1a`, color: d.color }}>
          <AzureIcon kind={d.type ? "resource" : "resource_group"} type={d.type} className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <span className="truncate text-[12px] font-semibold text-gray-800" title={d.name}>{d.name}</span>
            {azureView && reach === "public" && <span title="Publicly reachable">🌐</span>}
            {azureView && reach === "private" && <span title="Private only (no public access)">🔒</span>}
          </div>
          <div className="truncate text-[10px] text-gray-400">{d.type ? friendlyResourceType(d.type) : "concept"}</div>
        </div>
        {hosting && <span className={`shrink-0 rounded px-1 py-0.5 text-[8px] font-bold ${HOSTING_META[hosting].cls}`} title={`${HOSTING_META[hosting].label} (hosting model)`}>{HOSTING_META[hosting].label}</span>}
      </div>
      {(d.sku || meta.length > 0 || (azureView && cost > 0)) && (
        <div className="flex flex-wrap items-center gap-1 border-t border-gray-100 px-2.5 py-1">
          {d.sku && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-600">{d.sku}</span>}
          {meta.map(([k, v]) => (
            <span key={k} className="rounded bg-gray-50 px-1.5 py-0.5 text-[9px] text-gray-500" title={`${k}: ${v}`}>{v}</span>
          ))}
          {azureView && cost > 0 && <span className="ml-auto rounded bg-emerald-50 px-1.5 py-0.5 text-[9px] font-medium text-emerald-700" title="Rough indicative monthly cost (not billing)">{fmtUsd(cost)}/mo</span>}
        </div>
      )}
    </div>
  );
}
const nodeTypes = { azure: AzureNodeCard };

// Boundary container overlay (resource group / subscription boxes). Rendered inside the
// React Flow viewport so it pans/zooms with the canvas. Sits visually behind the nodes.
function BoundaryLayer({ boxes }: { boxes: { key: string; label: string; x: number; y: number; w: number; h: number; tone?: string }[] }) {
  const { x, y, zoom } = useViewport();
  if (boxes.length === 0) return null;
  return (
    <div className="pointer-events-none absolute left-0 top-0 z-0 h-full w-full overflow-visible">
      <div style={{ transform: `translate(${x}px, ${y}px) scale(${zoom})`, transformOrigin: "0 0" }} className="absolute left-0 top-0">
        {boxes.map((b) => {
          const tone = b.tone || "#0ea5e9";
          return (
            <div key={b.key} className="absolute rounded-xl border-2 border-dashed"
              style={{ left: b.x, top: b.y, width: b.w, height: b.h, borderColor: `${tone}b3`, background: `${tone}14` }}>
              <span className="absolute left-2 top-1 rounded px-1.5 py-0.5 text-[11px] font-medium" style={{ background: `${tone}26`, color: tone }}>⬚ {b.label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

let _seq = 0;
const newId = (p: string) => `${p}${Date.now().toString(36)}${(_seq++).toString(36)}`;

// --- Auto-layout engine: several intelligent arrangements -------------------------
// Approx. node footprint (incl. spacing) used by all layouts to avoid overlaps.
const NODE_W = 250;
const NODE_H = 135;

type Pos = { x: number; y: number };
type LayoutKind = "flow_v" | "flow_h" | "layered" | "grid" | "radial" | "force";

const LAYOUT_OPTIONS: { id: LayoutKind; label: string; icon: string; hint: string }[] = [
  { id: "flow_v", label: "Hierarchical ↓", icon: "↧", hint: "Top-down flow following connections" },
  { id: "flow_h", label: "Hierarchical →", icon: "↦", hint: "Left-to-right flow following connections" },
  { id: "layered", label: "Layered tiers", icon: "☰", hint: "Rows grouped by architectural tier" },
  { id: "grid", label: "Compact grid", icon: "▦", hint: "Uniform grid, clustered by category" },
  { id: "radial", label: "Radial", icon: "◎", hint: "Circular ring, clustered by category" },
  { id: "force", label: "Organic", icon: "✺", hint: "Force-directed spread to reduce overlaps" },
];

/** Layered by architectural tier (LAYER_ORDER), each tier a centered row. */
function layoutLayered(nodes: Node<AzData>[]): Map<string, Pos> {
  const byLayer = new Map<string, Node<AzData>[]>();
  for (const n of nodes) {
    const l = LAYER_ORDER.includes(n.data.layer) ? n.data.layer : "shared";
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l)!.push(n);
  }
  const rows = LAYER_ORDER.filter((l) => byLayer.has(l));
  const maxCount = Math.max(1, ...rows.map((l) => byLayer.get(l)!.length));
  const totalW = maxCount * NODE_W;
  const pos = new Map<string, Pos>();
  rows.forEach((layer, r) => {
    const g = byLayer.get(layer)!;
    const startX = (totalW - g.length * NODE_W) / 2;
    g.forEach((n, c) => pos.set(n.id, { x: startX + c * NODE_W, y: r * NODE_H }));
  });
  return pos;
}

/** Sugiyama-style hierarchical flow: rank nodes by edge direction, order each rank by
 *  barycenter to reduce crossings. `dir` = "v" (top-down) or "h" (left-right). */
function layoutFlow(nodes: Node<AzData>[], edges: Edge[], dir: "v" | "h"): Map<string, Pos> {
  const ids = nodes.map((n) => n.id);
  if (ids.length === 0) return new Map();
  const idSet = new Set(ids);
  const inAdj = new Map<string, string[]>();
  const outAdj = new Map<string, string[]>();
  for (const id of ids) { inAdj.set(id, []); outAdj.set(id, []); }
  for (const e of edges) {
    if (idSet.has(e.source) && idSet.has(e.target) && e.source !== e.target) {
      outAdj.get(e.source)!.push(e.target);
      inAdj.get(e.target)!.push(e.source);
    }
  }
  // Longest-path layering (relax up to n times; tolerates cycles).
  const rank = new Map<string, number>(ids.map((id) => [id, 0]));
  for (let iter = 0; iter < ids.length; iter++) {
    let changed = false;
    for (const id of ids) {
      let r = 0;
      for (const p of inAdj.get(id)!) r = Math.max(r, rank.get(p)! + 1);
      if (r !== rank.get(id)!) { rank.set(id, r); changed = true; }
    }
    if (!changed) break;
  }
  const byRank = new Map<number, string[]>();
  for (const id of ids) {
    const r = rank.get(id)!;
    if (!byRank.has(r)) byRank.set(r, []);
    byRank.get(r)!.push(id);
  }
  const ranks = [...byRank.keys()].sort((a, b) => a - b);
  // Barycenter ordering sweeps to reduce edge crossings.
  const order = new Map<string, number>();
  ranks.forEach((r) => byRank.get(r)!.forEach((id, i) => order.set(id, i)));
  for (let sweep = 0; sweep < 6; sweep++) {
    const downward = sweep % 2 === 0;
    const seq = downward ? ranks : [...ranks].reverse();
    for (const r of seq) {
      const group = byRank.get(r)!;
      const bary = (id: string) => {
        const rel = downward ? inAdj.get(id)! : outAdj.get(id)!;
        if (rel.length === 0) return order.get(id)!;
        return rel.reduce((s, p) => s + (order.get(p) ?? 0), 0) / rel.length;
      };
      group.sort((a, b) => bary(a) - bary(b));
      group.forEach((id, i) => order.set(id, i));
    }
  }
  const crossGap = dir === "v" ? NODE_W : NODE_H;
  const rankGap = dir === "v" ? NODE_H : NODE_W + 70;
  const maxInRank = Math.max(1, ...ranks.map((r) => byRank.get(r)!.length));
  const crossSpan = maxInRank * crossGap;
  const pos = new Map<string, Pos>();
  ranks.forEach((r, ri) => {
    const group = byRank.get(r)!;
    const start = (crossSpan - group.length * crossGap) / 2;
    group.forEach((id, i) => {
      const cross = start + i * crossGap;
      const along = ri * rankGap;
      pos.set(id, dir === "v" ? { x: cross, y: along } : { x: along, y: cross });
    });
  });
  return pos;
}

/** Compact near-square grid, ordered by tier then category so similar nodes cluster. */
function layoutGrid(nodes: Node<AzData>[]): Map<string, Pos> {
  const sorted = [...nodes].sort((a, b) => {
    const la = LAYER_ORDER.indexOf(a.data.layer), lb = LAYER_ORDER.indexOf(b.data.layer);
    if (la !== lb) return la - lb;
    return (a.data.category || "").localeCompare(b.data.category || "");
  });
  const cols = Math.max(1, Math.ceil(Math.sqrt(sorted.length)));
  const pos = new Map<string, Pos>();
  sorted.forEach((n, i) => pos.set(n.id, { x: (i % cols) * NODE_W, y: Math.floor(i / cols) * NODE_H }));
  return pos;
}

/** Single ring, ordered (clustered) by category. */
function layoutRadial(nodes: Node<AzData>[]): Map<string, Pos> {
  const sorted = [...nodes].sort((a, b) =>
    (a.data.category || "").localeCompare(b.data.category || "") ||
    (a.data.name || "").localeCompare(b.data.name || ""));
  const n = sorted.length;
  const pos = new Map<string, Pos>();
  if (n === 1) { pos.set(sorted[0].id, { x: 0, y: 0 }); return pos; }
  const radius = Math.max(280, (n * NODE_W) / (2 * Math.PI));
  sorted.forEach((node, i) => {
    const a = (i / n) * 2 * Math.PI - Math.PI / 2;
    pos.set(node.id, { x: radius + radius * Math.cos(a), y: radius + radius * Math.sin(a) });
  });
  return pos;
}

/** Fruchterman–Reingold force-directed layout (organic; separates clusters). */
function layoutForce(nodes: Node<AzData>[], edges: Edge[]): Map<string, Pos> {
  const ids = nodes.map((n) => n.id);
  const n = ids.length;
  if (n === 0) return new Map();
  if (n === 1) return new Map([[ids[0], { x: 0, y: 0 }]]);
  const idSet = new Set(ids);
  const W = Math.max(900, Math.sqrt(n) * 360);
  const k = W / Math.sqrt(n);
  const p = new Map<string, Pos>();
  const cols = Math.ceil(Math.sqrt(n));
  nodes.forEach((node, i) => p.set(node.id, {
    x: (i % cols) * k + Math.random() * 12,
    y: Math.floor(i / cols) * k + Math.random() * 12,
  }));
  const elist = edges.filter((e) => idSet.has(e.source) && idSet.has(e.target) && e.source !== e.target);
  let temp = W / 8;
  for (let it = 0; it < 320; it++) {
    const disp = new Map<string, Pos>(ids.map((id) => [id, { x: 0, y: 0 }]));
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const pa = p.get(ids[i])!, pb = p.get(ids[j])!;
      const dx = pa.x - pb.x, dy = pa.y - pb.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const rep = (k * k) / dist;
      const ux = dx / dist, uy = dy / dist;
      const da = disp.get(ids[i])!, db = disp.get(ids[j])!;
      da.x += ux * rep; da.y += uy * rep; db.x -= ux * rep; db.y -= uy * rep;
    }
    for (const e of elist) {
      const pa = p.get(e.source)!, pb = p.get(e.target)!;
      const dx = pa.x - pb.x, dy = pa.y - pb.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const att = (dist * dist) / k;
      const ux = dx / dist, uy = dy / dist;
      const da = disp.get(e.source)!, db = disp.get(e.target)!;
      da.x -= ux * att; da.y -= uy * att; db.x += ux * att; db.y += uy * att;
    }
    for (const id of ids) {
      const d = disp.get(id)!, pp = p.get(id)!;
      const len = Math.hypot(d.x, d.y) || 0.01;
      pp.x += (d.x / len) * Math.min(len, temp);
      pp.y += (d.y / len) * Math.min(len, temp);
    }
    temp *= 0.97;
  }
  return p;
}

function computeLayout(kind: LayoutKind, nodes: Node<AzData>[], edges: Edge[]): Map<string, Pos> {
  switch (kind) {
    case "layered": return layoutLayered(nodes);
    case "flow_v": return layoutFlow(nodes, edges, "v");
    case "flow_h": return layoutFlow(nodes, edges, "h");
    case "grid": return layoutGrid(nodes);
    case "radial": return layoutRadial(nodes);
    case "force": return layoutForce(nodes, edges);
  }
}

// --- Best-practice linter: deterministic Well-Architected heuristics over the diagram.
// These are advisory hints inferred from the node/edge graph (no Azure calls), surfaced
// as on-canvas badges + a findings panel. ---------------------------------------------
export type LintSeverity = "warning" | "suggestion";
export interface LintFinding {
  id: string;
  nodeId: string | null; // null = diagram-level
  severity: LintSeverity;
  title: string;
  detail: string;
}

const _t = (n: Node<AzData>) => (n.data.type || "").toLowerCase();
const _isType = (n: Node<AzData>, ...frags: string[]) => frags.some((f) => _t(n).includes(f));

export function computeLint(nodes: Node<AzData>[], edges: Edge[]): LintFinding[] {
  const out: LintFinding[] = [];
  if (nodes.length === 0) return out;
  const neighborsOf = (id: string) => {
    const set = new Set<string>();
    for (const e of edges) {
      if (e.source === id) set.add(e.target);
      if (e.target === id) set.add(e.source);
    }
    return [...set].map((nid) => nodes.find((n) => n.id === nid)).filter(Boolean) as Node<AzData>[];
  };
  const has = (...frags: string[]) => nodes.some((n) => _isType(n, ...frags));
  const hasPrivateEndpoint = (n: Node<AzData>) =>
    neighborsOf(n.id).some((m) => _isType(m, "privateendpoint"));

  // Per-node data-exposure & resilience checks.
  for (const n of nodes) {
    const name = n.data.name || _t(n).split("/").pop() || "resource";
    if (_isType(n, "storage/storageaccounts", "sql/servers", "documentdb", "keyvault/vaults", "cache/redis") && !hasPrivateEndpoint(n)) {
      out.push({ id: `pe:${n.id}`, nodeId: n.id, severity: "warning", title: "No private endpoint", detail: `${name} has no Private Endpoint on the diagram — it may be reachable over the public internet. Consider Private Link.` });
    }
    if (_isType(n, "compute/virtualmachines")) {
      const peers = neighborsOf(n.id);
      const protectedByBackup = peers.some((m) => _isType(m, "recoveryservices"));
      if (!protectedByBackup) out.push({ id: `bk:${n.id}`, nodeId: n.id, severity: "warning", title: "No backup", detail: `${name} isn't connected to a Recovery Services Vault. Add Azure Backup for resilience.` });
    }
    if (_isType(n, "web/sites", "web/staticsites") && !neighborsOf(n.id).some((m) => _isType(m, "applicationgateways", "frontdoors", "cdn/profiles", "apimanagement"))) {
      out.push({ id: `waf:${n.id}`, nodeId: n.id, severity: "suggestion", title: "No gateway/WAF in front", detail: `${name} has no Application Gateway, Front Door, or APIM in front — consider a WAF for protection.` });
    }
    if (_isType(n, "network/publicipaddresses")) {
      const attached = neighborsOf(n.id).some((m) => _isType(m, "virtualmachines", "loadbalancers", "applicationgateways", "natgateways", "bastionhosts", "azurefirewalls"));
      if (!attached) out.push({ id: `pip:${n.id}`, nodeId: n.id, severity: "suggestion", title: "Unattached public IP", detail: `${name} doesn't appear attached to anything — it may be wasted cost and an exposure risk.` });
    }
  }

  // Diagram-level WAF pillars.
  const hasCompute = has("virtualmachines", "web/sites", "containerservice", "app/containerapps", "web/sites/functions");
  if (hasCompute && !has("insights/components", "operationalinsights/workspaces", "insights/")) {
    out.push({ id: "mon:diagram", nodeId: null, severity: "warning", title: "No monitoring", detail: "No Application Insights or Log Analytics on the diagram — add observability for Operational Excellence." });
  }
  if (nodes.length > 2 && !has("managedidentity", "keyvault/vaults")) {
    out.push({ id: "id:diagram", nodeId: null, severity: "suggestion", title: "No Key Vault / Managed Identity", detail: "Consider a Key Vault and Managed Identities for secret-free, secure access between resources." });
  }
  return out;
}

// --- ARM-type combobox: pick from the catalog (icons + display names), or type a custom
// resource type if it isn't in the curated list. --------------------------------------
function ArmTypeCombobox({ value, palette, onChange }: {
  value: string;
  palette: ArchitecturePaletteItem[];
  onChange: (type: string, category?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as globalThis.Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  const q = query.trim().toLowerCase();
  const matches = palette
    .filter((p) => !q || p.label.toLowerCase().includes(q) || p.type.toLowerCase().includes(q))
    .slice(0, 50);
  const custom = query.trim();
  const exact = palette.some((p) => p.type === custom.toLowerCase());

  function pick(type: string, category?: string) {
    onChange(type, category);
    setOpen(false);
    setQuery("");
  }

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => { setOpen((v) => !v); setQuery(""); }}
        className="flex w-full items-center gap-1.5 rounded border px-2 py-1 text-left text-[11px] hover:bg-gray-50">
        <AzureIcon kind="resource" type={value} className="h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate">{value ? friendlyResourceType(value) : "Select type…"}</span>
        <span className="shrink-0 text-gray-400">▾</span>
      </button>
      {value && <div className="mt-0.5 truncate font-mono text-[9px] text-gray-400" title={value}>{value}</div>}
      {open && (
        <div className="absolute left-0 right-0 z-30 mt-1 rounded-lg border bg-white shadow-xl">
          <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search or paste an ARM type…"
            className="w-full rounded-t-lg border-b px-2 py-1.5 text-[11px] focus:outline-none"
            onKeyDown={(e) => { if (e.key === "Enter" && custom && !exact) pick(custom.toLowerCase()); if (e.key === "Escape") setOpen(false); }} />
          <div className="max-h-56 overflow-y-auto py-1">
            {matches.map((p) => (
              <button type="button" key={p.type} onClick={() => pick(p.type, p.category)}
                className={`flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-gray-50 ${p.type === value ? "bg-brand/5" : ""}`}>
                <AzureIcon kind="resource" type={p.type} className="h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[11px] text-gray-700">{p.label}</span>
                  <span className="block truncate font-mono text-[9px] text-gray-400">{p.type}</span>
                </span>
                {p.type === value && <span className="shrink-0 text-brand">✓</span>}
              </button>
            ))}
            {matches.length === 0 && !custom && <div className="px-2 py-2 text-[11px] text-gray-400">Type to search…</div>}
            {custom && !exact && (
              <button type="button" onClick={() => pick(custom.toLowerCase())}
                className="flex w-full items-center gap-2 border-t px-2 py-1.5 text-left hover:bg-gray-50">
                <AzureIcon kind="resource" type={custom} className="h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1 truncate text-[11px] text-gray-700">Use custom type: <span className="font-mono text-gray-500">{custom.toLowerCase()}</span></span>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// --- the editor -------------------------------------------------------------
function CanvasInner({
  arch, catalog, onSaved,
}: {
  arch: Architecture;
  catalog: ArchitectureCatalog | undefined;
  onSaved: (a: Architecture) => void;
}) {
  const colorOf = useCallback(
    (cat: string) => catalog?.categories.find((c) => c.id === cat)?.color ?? "#6b7280",
    [catalog],
  );
  const [nodes, setNodes] = useState<Node<AzData>[]>(() => toFlowNodes(arch, colorOf));
  const [edges, setEdges] = useState<Edge[]>(() => toFlowEdges(arch));
  const [groups, setGroups] = useState<ArchGroup[]>(arch.groups || []);
  const [name, setName] = useState(arch.name);
  const [selNode, setSelNode] = useState<string | null>(null);
  const [selEdge, setSelEdge] = useState<string | null>(null);
  // Private Network Reachability Analyzer: open the Test-connectivity modal, optionally
  // preset with the clicked node as the target.
  const [netCheck, setNetCheck] = useState<{ targetNodeId?: string; targetHost?: string } | null>(null);
  const [dnsDebug, setDnsDebug] = useState<{ fqdn?: string } | null>(null);
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; nodeId: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [aiOpen, setAiOpen] = useState(false);
  const [aiGoal, setAiGoal] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [tidyOpen, setTidyOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  // Collapse the whole Resources palette sidebar to a thin rail (more canvas room).
  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  // Visio-style tools: snap-to-grid (on by default), connector routing style, align popover.
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [routing, setRouting] = useState<"bezier" | "smoothstep" | "step" | "straight">("bezier");
  const [alignOpen, setAlignOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  // Clipboard for copy/paste of selected nodes (+ their internal edges).
  const clipboardRef = useRef<{ nodes: ArchNode[]; edges: ArchEdge[] } | null>(null);
  // Collapsed palette categories (by category id). A category collapses to just its header.
  const [collapsedCats, setCollapsedCats] = useState<Set<string>>(new Set());
  // Phase 1 analysis/view features.
  const [search, setSearch] = useState("");
  const [searchFocus, setSearchFocus] = useState(false);
  const [hiddenCats, setHiddenCats] = useState<Set<string>>(new Set());
  const [filterOpen, setFilterOpen] = useState(false);
  const [impactOn, setImpactOn] = useState(false);
  const [lintOpen, setLintOpen] = useState(true);
  const [boundaryMode, setBoundaryMode] = useState<"none" | "resource_group" | "subscription" | "vnet" | "subnet" | "region">("none");
  const [assessOn, setAssessOn] = useState(false);
  // Azure-semantics view: reachability badges, hosting-model tags, indicative cost.
  const [azureView, setAzureView] = useState(true);
  // Hosting-model filter ("" = all): IaaS | PaaS | SaaS | Net.
  const [hostingFilter, setHostingFilter] = useState("");
  // Well-Architected pillar overlay tint ("" = off).
  const [pillarOverlay, setPillarOverlay] = useState("");
  // Ingress path tracing: when on + a node selected, highlight the directed north-south path.
  const [pathMode, setPathMode] = useState(false);
  // Latest assessment for this workload: arm_id(lowercased) -> { severity, findings }.
  const [assessMap, setAssessMap] = useState<Map<string, { severity: string; findings: AssessmentFinding[] }>>(new Map());
  const [assessScore, setAssessScore] = useState<number | null>(null);
  const [assessReady, setAssessReady] = useState(false);
  const [presentMode, setPresentMode] = useState(false);
  // Phase 6/7: drift + AI Q&A.
  const [driftBusy, setDriftBusy] = useState(false);
  const [drift, setDrift] = useState<import("../api").ArchitectureDrift | null>(null);
  const [askOpen, setAskOpen] = useState(false);
  const [askQ, setAskQ] = useState("");
  const [askA, setAskA] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const rf = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);
  // Live viewport (pan/zoom) so the inspector can be positioned next to the selected node.
  const viewport = useViewport();
  const navigate = useNavigate();

  // --- Undo / redo (declared early so the useCallback mutators below can call it) ------
  // History of model snapshots. A live ref mirror of state lets pushHistory stay stable
  // (no dependency churn) so the useCallback mutators can call it safely.
  type Snap = { nodes: ArchNode[]; edges: ArchEdge[]; groups: ArchGroup[]; name: string };
  const stateRef = useRef({ nodes, edges, groups, name });
  useEffect(() => { stateRef.current = { nodes, edges, groups, name }; }, [nodes, edges, groups, name]);
  const histRef = useRef<{ past: Snap[]; future: Snap[] }>({ past: [], future: [] });
  const [histVer, setHistVer] = useState(0);
  const snapshot = useCallback((): Snap => {
    const s = stateRef.current;
    return {
      nodes: s.nodes.map((n) => ({ ...(n.data as AzData), id: n.id, x: n.position.x, y: n.position.y } as ArchNode)),
      edges: s.edges.map((e) => ({ id: e.id, source: e.source, target: e.target, label: typeof e.label === "string" ? e.label : "", kind: ((e.data?.kind as ArchEdgeKind) ?? "connects_to"), dashed: Boolean(e.data?.dashed) })),
      groups: s.groups, name: s.name,
    };
  }, []);
  const pushHistory = useCallback(() => {
    histRef.current.past.push(snapshot());
    if (histRef.current.past.length > 100) histRef.current.past.shift();
    histRef.current.future = [];
    setHistVer((v) => v + 1);
  }, [snapshot]);
  const restoreSnap = useCallback((s: Snap) => {
    const a = { ...arch, name: s.name, nodes: s.nodes, edges: s.edges, groups: s.groups } as Architecture;
    setName(s.name); setGroups(s.groups);
    setNodes(toFlowNodes(a, colorOf)); setEdges(toFlowEdges(a));
    setSelNode(null); setSelEdge(null); setDirty(true);
  }, [arch, colorOf]);
  const undo = useCallback(() => {
    const h = histRef.current; if (!h.past.length) return;
    h.future.push(snapshot()); restoreSnap(h.past.pop()!); setHistVer((v) => v + 1);
  }, [snapshot, restoreSnap]);
  const redo = useCallback(() => {
    const h = histRef.current; if (!h.future.length) return;
    h.past.push(snapshot()); restoreSnap(h.future.pop()!); setHistVer((v) => v + 1);
  }, [snapshot, restoreSnap]);
  const canUndo = histRef.current.past.length > 0;
  const canRedo = histRef.current.future.length > 0;
  void histVer; // re-render trigger for the button disabled state
  const selIdsRef = useRef<{ nodes: string[]; edges: string[] }>({ nodes: [], edges: [] });

  const onNodesChange = useCallback((c: NodeChange[]) => { setNodes((n) => applyNodeChanges(c, n) as Node<AzData>[]); if (c.some((x) => x.type === "position" || x.type === "remove")) setDirty(true); }, []);
  const onEdgesChange = useCallback((c: EdgeChange[]) => { setEdges((e) => applyEdgeChanges(c, e)); if (c.some((x) => x.type === "remove")) setDirty(true); }, []);
  const onConnect = useCallback((conn: Connection) => {
    pushHistory();
    setEdges((eds) => addEdge({
      ...conn,
      id: newId("e"),
      markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor("connects_to") },
      style: { stroke: edgeColor("connects_to"), strokeWidth: 1.6 },
      data: { kind: "connects_to", dashed: false },
    }, eds));
    setDirty(true);
  }, [pushHistory]);

  // Drag a palette item onto the canvas → new node.
  const onDragOver = useCallback((e: React.DragEvent) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }, []);
  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const raw = e.dataTransfer.getData("application/architecture-node");
    if (!raw) return;
    pushHistory();
    const item = JSON.parse(raw) as ArchitecturePaletteItem;
    const pos = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const id = newId("n");
    const node: Node<AzData> = {
      id, type: "azure", position: pos,
      data: {
        id, arm_id: "", name: item.label, type: item.type, category: item.category,
        layer: layerFor(item.category), resource_group: "", subscription_id: "", location: "",
        sku: "", meta: {}, group_id: "", x: pos.x, y: pos.y, color: colorOf(item.category),
      },
    };
    setNodes((n) => [...n, node]);
    setDirty(true);
  }, [rf, colorOf, pushHistory]);

  function layerFor(cat: string): string {
    const map: Record<string, string> = {
      web: "presentation", compute: "application", containers: "application", ai: "application",
      integration: "integration", data: "data", storage: "data", analytics: "data",
      networking: "networking", security: "security", monitoring: "monitoring",
    };
    return map[cat] ?? "shared";
  }

  // Tidy: re-arrange the diagram with one of several intelligent auto-layouts.
  const applyLayout = useCallback((kind: LayoutKind) => {
    pushHistory();
    setNodes((cur) => {
      const pos = computeLayout(kind, cur, edges);
      return cur.map((n) => (pos.has(n.id) ? { ...n, position: pos.get(n.id)! } : n));
    });
    setDirty(true);
    setTidyOpen(false);
    setTimeout(() => rf.fitView({ padding: 0.2, duration: 400 }), 60);
  }, [edges, rf, pushHistory]);

  function modelFromState(): Partial<Architecture> {
    const nodeOut: ArchNode[] = nodes.map((n) => ({
      ...(n.data as AzData), id: n.id, x: n.position.x, y: n.position.y,
    } as ArchNode));
    const edgeOut: ArchEdge[] = edges.map((e) => ({
      id: e.id, source: e.source, target: e.target,
      label: typeof e.label === "string" ? e.label : "",
      kind: ((e.data?.kind as ArchEdgeKind) ?? "connects_to"),
      dashed: Boolean(e.data?.dashed),
    }));
    return { id: arch.id, name, nodes: nodeOut, edges: edgeOut, groups, source: arch.source };
  }

  async function save() {
    setSaving(true); setMsg("");
    try {
      const res = await api.upsertArchitecture(modelFromState());
      setDirty(false);
      onSaved(res.architecture);
      setMsg("Saved.");
      setTimeout(() => setMsg(""), 1500);
    } catch (e) { setMsg(formatError(e)); } finally { setSaving(false); }
  }

  async function runEnhance() {
    if (!aiGoal.trim()) return;
    setAiBusy(true); setMsg("");
    try {
      // Persist current edits first so the AI refines the latest diagram.
      await api.upsertArchitecture(modelFromState());
      const res = await api.enhanceArchitecture(arch.id, aiGoal.trim());
      const a = res.architecture;
      setName(a.name);
      setGroups(a.groups || []);
      setNodes(toFlowNodes(a, colorOf));
      setEdges(toFlowEdges(a));
      setDirty(false);
      setAiOpen(false); setAiGoal("");
      onSaved(a);
      setTimeout(() => rf.fitView({ padding: 0.2, duration: 400 }), 60);
    } catch (e) { setMsg(formatError(e)); } finally { setAiBusy(false); }
  }

  // Update the selected node/edge from the inspector.
  const updNode = (id: string, patch: Partial<AzData>) => {
    pushHistory();
    setNodes((cur) => cur.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch, color: patch.category ? colorOf(patch.category) : n.data.color } } : n)));
    setDirty(true);
  };
  const updEdge = (id: string, patch: { label?: string; kind?: ArchEdgeKind; dashed?: boolean }) => {
    pushHistory();
    setEdges((cur) => cur.map((e) => {
      if (e.id !== id) return e;
      const kind = patch.kind ?? (e.data?.kind as ArchEdgeKind) ?? "connects_to";
      const dashed = patch.dashed ?? Boolean(e.data?.dashed);
      return {
        ...e,
        label: patch.label !== undefined ? patch.label : e.label,
        animated: kind === "data_flow",
        markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(kind) },
        style: { stroke: edgeColor(kind), strokeWidth: 1.6, strokeDasharray: dashed ? "5 4" : undefined },
        data: { kind, dashed },
      };
    }));
    setDirty(true);
  };
  const deleteSel = () => {
    pushHistory();
    if (selNode) { setNodes((n) => n.filter((x) => x.id !== selNode)); setEdges((e) => e.filter((x) => x.source !== selNode && x.target !== selNode)); setSelNode(null); setDirty(true); }
    if (selEdge) { setEdges((e) => e.filter((x) => x.id !== selEdge)); setSelEdge(null); setDirty(true); }
  };

  // Warn on unsaved navigation away.
  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  // Phase 3: fetch the latest completed assessment for this workload, building a map of
  // arm_id -> worst failing severity + the findings, to overlay on the diagram nodes.
  useEffect(() => {
    let cancelled = false;
    const wid = arch.workload_id;
    if (!wid) { setAssessReady(true); return; }
    (async () => {
      try {
        const runs = (await api.assessmentRuns(wid)).runs.filter((r) => r.status === "succeeded");
        if (!runs.length) { if (!cancelled) setAssessReady(true); return; }
        const latest = runs.sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""))[0];
        const detail = (await api.assessmentRun(latest.id)).run;
        if (cancelled) return;
        const rank: Record<string, number> = { info: 1, warning: 2, error: 3, critical: 4 };
        const m = new Map<string, { severity: string; findings: AssessmentFinding[] }>();
        for (const f of detail.findings || []) {
          if (f.status !== "fail") continue;
          for (const r of f.flagged_resources || []) {
            const key = (r.id || "").toLowerCase();
            if (!key) continue;
            const cur = m.get(key);
            if (!cur) m.set(key, { severity: f.severity, findings: [f] });
            else { cur.findings.push(f); if ((rank[f.severity] || 0) > (rank[cur.severity] || 0)) cur.severity = f.severity; }
          }
        }
        setAssessMap(m);
        setAssessScore(detail.overall_score);
        setAssessReady(true);
      } catch { if (!cancelled) setAssessReady(true); }
    })();
    return () => { cancelled = true; };
  }, [arch.workload_id]);

  const selectedNode = nodes.find((n) => n.id === selNode)?.data;
  const selectedEdge = edges.find((e) => e.id === selEdge);

  // Position the inspector panel next to the selected node/edge (in canvas-wrapper
  // coordinates), preferring the node's right side and flipping/clamping to stay in view.
  const inspectorPos = useMemo(() => {
    const PANEL_W = 240, GAP = 12, MARGIN = 8;
    const wrap = wrapRef.current?.getBoundingClientRect();
    const z = viewport.zoom;
    let anchor: { x: number; y: number; w: number; h: number } | null = null;
    if (selNode) {
      const n = nodes.find((nd) => nd.id === selNode);
      if (n) anchor = { x: n.position.x, y: n.position.y, w: (n.measured?.width ?? 200), h: (n.measured?.height ?? 70) };
    } else if (selEdge) {
      const e = edges.find((ed) => ed.id === selEdge);
      const s = e && nodes.find((nd) => nd.id === e.source);
      const t = e && nodes.find((nd) => nd.id === e.target);
      if (s && t) anchor = { x: (s.position.x + t.position.x) / 2, y: (s.position.y + t.position.y) / 2, w: 0, h: 0 };
    }
    if (!anchor || !wrap) return null;
    // Flow → screen (within wrapper): apply viewport transform.
    const nodeLeft = anchor.x * z + viewport.x;
    const nodeTop = anchor.y * z + viewport.y;
    const nodeW = anchor.w * z;
    let left = nodeLeft + nodeW + GAP;
    if (left + PANEL_W > wrap.width - MARGIN) left = nodeLeft - PANEL_W - GAP; // flip to left
    left = Math.max(MARGIN, Math.min(left, wrap.width - PANEL_W - MARGIN));
    // Vertically: start aligned to the node, but clamp so the panel fits, and cap its
    // height to the remaining space so a long panel scrolls instead of overflowing.
    const avail = wrap.height - 2 * MARGIN;
    let top = nodeTop;
    top = Math.max(MARGIN, Math.min(top, wrap.height - MARGIN - Math.min(360, avail)));
    const maxHeight = wrap.height - top - MARGIN;
    return { left, top, maxHeight };
  }, [selNode, selEdge, nodes, edges, viewport]);

  // Best-practice lint findings (deterministic), indexed by node for on-card badges.
  const lint = useMemo(() => computeLint(nodes, edges), [nodes, edges]);
  const lintByNode = useMemo(() => {
    const m = new Map<string, LintFinding[]>();
    for (const f of lint) if (f.nodeId) (m.get(f.nodeId) ?? m.set(f.nodeId, []).get(f.nodeId)!).push(f);
    return m;
  }, [lint]);

  // Blast-radius: when "Impact" is on and a node is selected, the set of nodes reachable
  // up- AND down-stream from it (everything else is dimmed).
  const impactIds = useMemo(() => {
    if (!impactOn || !selNode) return null as Set<string> | null;
    const out = new Map<string, string[]>(), inc = new Map<string, string[]>();
    for (const e of edges) {
      (out.get(e.source) ?? out.set(e.source, []).get(e.source)!).push(e.target);
      (inc.get(e.target) ?? inc.set(e.target, []).get(e.target)!).push(e.source);
    }
    const reach = (adj: Map<string, string[]>) => {
      const seen = new Set<string>(), q = [selNode];
      while (q.length) { const c = q.shift()!; for (const nb of adj.get(c) ?? []) if (!seen.has(nb)) { seen.add(nb); q.push(nb); } }
      return seen;
    };
    return new Set<string>([selNode, ...reach(out), ...reach(inc)]);
  }, [impactOn, selNode, edges]);

  // Ingress path tracing: when "Path" is on and a node is selected, the directed set of
  // nodes DOWNSTREAM of it (the north-south request path), with the edges on that path.
  const pathSet = useMemo(() => {
    if (!pathMode || !selNode) return null as { nodes: Set<string>; edges: Set<string> } | null;
    const out = new Map<string, { t: string; e: string }[]>();
    for (const e of edges) (out.get(e.source) ?? out.set(e.source, []).get(e.source)!).push({ t: e.target, e: e.id });
    const seenN = new Set<string>([selNode]); const seenE = new Set<string>(); const q = [selNode];
    while (q.length) { const c = q.shift()!; for (const { t, e } of out.get(c) ?? []) { seenE.add(e); if (!seenN.has(t)) { seenN.add(t); q.push(t); } } }
    return { nodes: seenN, edges: seenE };
  }, [pathMode, selNode, edges]);

  // PaaS resources reachable privately (a Private Endpoint node points at them) — drives
  // the public/private reachability badge.
  const peLinkedIds = useMemo(() => {
    const set = new Set<string>();
    const peNodes = nodes.filter((n) => isPrivateEndpointType(n.data.type));
    for (const pe of peNodes) for (const e of edges) {
      if (e.source === pe.id) set.add(e.target);
      if (e.target === pe.id) set.add(e.source);
    }
    return set;
  }, [nodes, edges]);

  // Pillar overlay: arm_id(lowercased) -> tint color, from failing assessment findings in
  // the selected Well-Architected pillar.
  const PILLAR_TINT: Record<string, string> = { security: "#dc2626", reliability: "#2563eb", cost: "#16a34a", operations: "#9333ea", performance: "#d97706" };
  const pillarTintByArm = useMemo(() => {
    const m = new Map<string, string>();
    if (!pillarOverlay) return m;
    for (const [arm, v] of assessMap) {
      if (v.findings.some((f) => (f.pillar || "").toLowerCase() === pillarOverlay)) m.set(arm, PILLAR_TINT[pillarOverlay] || "#dc2626");
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pillarOverlay, assessMap]);

  // Rendered nodes: apply category + hosting-model filters (hide), blast-radius / path
  // dimming, lint badges, and the Azure-semantics overlays (reachability, cost, pillar).
  const displayNodes = useMemo(() => nodes.map((n) => {
    const isHidden = hiddenCats.has(n.data.category) || (!!hostingFilter && hostingModel(n.data.type) !== hostingFilter && n.data.type !== "__note__");
    const activeSet = pathSet ? pathSet.nodes : (impactIds ?? null);
    const dim = activeSet ? !activeSet.has(n.id) : false;
    const findings = lintByNode.get(n.id);
    const assess = assessOn ? assessMap.get((n.data.arm_id || "").toLowerCase()) : undefined;
    // Reachability: private if PE-linked or publicNetworkAccess=Disabled; public if a
    // public-edge type, has a public IP, or publicNetworkAccess=Enabled on exposable PaaS.
    const pna = String(n.data.meta?.publicNetworkAccess ?? "").toLowerCase();
    let reach = "";
    if (peLinkedIds.has(n.id) || pna === "disabled" || isPrivateEndpointType(n.data.type)) reach = "private";
    else if (isPublicType(n.data.type) || pna === "enabled" || (isExposablePaaS(n.data.type) && pna !== "disabled" && !peLinkedIds.has(n.id))) reach = "public";
    const cost = azureView ? estMonthlyCost(n.data.type, n.data.sku) : 0;
    const pillarTint = pillarTintByArm.get((n.data.arm_id || "").toLowerCase()) || "";
    return {
      ...n,
      hidden: isHidden,
      data: { ...n.data, _dim: dim, _lintCount: findings?.length ?? 0, _lintSev: findings?.some((f) => f.severity === "warning") ? "warning" : findings?.length ? "suggestion" : "", _assessSev: assess?.severity ?? "", _assessCount: assess?.findings.length ?? 0, _azureView: azureView, _reach: reach, _cost: cost, _pillarTint: pillarTint },
    } as Node<AzData>;
  }), [nodes, hiddenCats, hostingFilter, impactIds, pathSet, lintByNode, assessOn, assessMap, azureView, peLinkedIds, pillarTintByArm]);

  // Total indicative monthly cost across visible nodes (Azure view).
  const hiddenNodeIds = useMemo(() => new Set(nodes.filter((n) => hiddenCats.has(n.data.category) || (!!hostingFilter && hostingModel(n.data.type) !== hostingFilter && n.data.type !== "__note__")).map((n) => n.id)), [nodes, hiddenCats, hostingFilter]);

  // Highlight the selected edge with an animated dotted brand-colored line so the user
  // can see which connection the inspector on the right is editing. Also hide edges whose
  // endpoints are filtered out, and dim edges outside the blast-radius. Render-only
  // (modelFromState reads source/target/label/kind/dashed, never `style`).
  const displayEdges = useMemo(() => edges.map((e) => {
    const routed = routing === "bezier" ? undefined : routing;
    const hidden = hiddenNodeIds.has(e.source) || hiddenNodeIds.has(e.target);
    const activeSet = pathSet ? pathSet.nodes : (impactIds ?? null);
    const dim = activeSet ? !(activeSet.has(e.source) && activeSet.has(e.target)) : false;
    const onPath = pathSet ? pathSet.edges.has(e.id) : false;
    if (e.id === selEdge) {
      return {
        ...e, type: routed, hidden, animated: true, zIndex: 10, selected: true,
        style: { ...(e.style as object), stroke: "#1f6feb", strokeWidth: 2.6, strokeDasharray: "1 5", strokeLinecap: "round" as const },
        markerEnd: { type: MarkerType.ArrowClosed, color: "#1f6feb" },
        labelStyle: { fontSize: 10, fill: "#1f6feb", fontWeight: 600 },
        labelBgStyle: { fill: "#e7f0fe", fillOpacity: 1 },
        labelBgPadding: [4, 3] as [number, number], labelBgBorderRadius: 4,
      };
    }
    // Highlight north-south ingress path edges in green and animated.
    if (onPath) {
      return {
        ...e, type: routed, hidden, animated: true, zIndex: 9,
        style: { ...(e.style as object), stroke: "#16a34a", strokeWidth: 3, opacity: 1 },
        markerEnd: { type: MarkerType.ArrowClosed, color: "#16a34a" },
      };
    }
    // Azure-semantics styling: VNet peering (thick teal), private link (dashed indigo),
    // gateway connections, and identity edges (dashed red).
    const srcN = nodes.find((n) => n.id === e.source)?.data;
    const tgtN = nodes.find((n) => n.id === e.target)?.data;
    const kind = (e.data?.kind as string) || "";
    let azStyle: Record<string, unknown> | null = null;
    let azMarker: string | null = null;
    if (azureView && srcN && tgtN) {
      if (isVNet(srcN.type) && isVNet(tgtN.type)) { azStyle = { stroke: "#0d9488", strokeWidth: 3 }; azMarker = "#0d9488"; }
      else if (isPrivateEndpointType(srcN.type) || isPrivateEndpointType(tgtN.type)) { azStyle = { stroke: "#6366f1", strokeWidth: 2, strokeDasharray: "2 3" }; azMarker = "#6366f1"; }
      else if (isGatewayType(srcN.type) || isGatewayType(tgtN.type)) { azStyle = { stroke: "#0891b2", strokeWidth: 2.4 }; azMarker = "#0891b2"; }
      else if (kind === "identity") { azStyle = { stroke: "#b91c1c", strokeWidth: 1.6, strokeDasharray: "4 3" }; azMarker = "#b91c1c"; }
    }
    if (azStyle) {
      return { ...e, type: routed, hidden, animated: false, style: { ...(e.style as object), ...azStyle, opacity: dim ? 0.12 : 1 }, markerEnd: azMarker ? { type: MarkerType.ArrowClosed, color: azMarker } : e.markerEnd };
    }
    if (hidden || dim) return { ...e, type: routed, hidden, style: { ...(e.style as object), opacity: dim ? 0.12 : 1 } };
    return { ...e, type: routed };
  }), [edges, selEdge, hiddenNodeIds, impactIds, pathSet, routing, azureView, nodes]);


  // Search matches (by name / type), for the find-a-node box.
  const searchMatches = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return [] as Node<AzData>[];
    return nodes.filter((n) => (n.data.name || "").toLowerCase().includes(q) || (n.data.type || "").toLowerCase().includes(q)).slice(0, 8);
  }, [nodes, search]);

  function focusNode(id: string) {
    setSelNode(id); setSelEdge(null); setSearchFocus(false);
    const n = rf.getNode(id);
    if (n) rf.setCenter(n.position.x + 110, n.position.y + 45, { zoom: 1.1, duration: 500 });
  }

  // Open the Test-connectivity modal targeting a node (its private fqdn/ip from meta).
  function openNetCheck(nodeId: string) {
    const data = nodes.find((n) => n.id === nodeId)?.data;
    const meta = (data?.meta || {}) as Record<string, unknown>;
    const targetHost = String(meta.fqdn || meta.private_ip || meta.ip || "") || (data?.name ?? "");
    setCtxMenu(null);
    setNetCheck({ targetNodeId: nodeId, targetHost });
  }

  // Open the DNS resolution debugger for a node (its fqdn from meta).
  function openDnsDebug(nodeId: string) {
    const data = nodes.find((n) => n.id === nodeId)?.data;
    const meta = (data?.meta || {}) as Record<string, unknown>;
    const fqdn = String(meta.fqdn || meta.private_ip || "") || (data?.name ?? "");
    setCtxMenu(null);
    setDnsDebug({ fqdn });
  }

  // Analyze telemetry: hand off the architecture's workload to Telemetry Intelligence.
  function openTeleIntel(nodeId: string) {
    const data = nodes.find((n) => n.id === nodeId)?.data;
    setCtxMenu(null);
    try {
      sessionStorage.setItem(
        "azsup.teleintelHandoff",
        JSON.stringify({ workloadId: arch.workload_id || "", nodeName: data?.name ?? "" }),
      );
    } catch {
      /* ignore */
    }
    navigate("/telemetry-intel");
  }

  // Drill-back from Telemetry/Monitoring Coverage: a one-shot sessionStorage handoff
  // {armId|nodeId} focuses the matching node once this architecture's nodes are loaded.
  useEffect(() => {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem("azsup.canvasFocus"); } catch { return; }
    if (!raw) return;
    let h: { armId?: string; nodeId?: string };
    try { h = JSON.parse(raw); } catch { return; }
    const target = h.nodeId
      ? nodes.find((n) => n.id === h.nodeId)
      : nodes.find((n) => (n.data.arm_id || "").toLowerCase() === (h.armId || "").toLowerCase());
    if (target) {
      try { sessionStorage.removeItem("azsup.canvasFocus"); } catch { /* ignore */ }
      setTimeout(() => focusNode(target.id), 300);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length]);

  // Present mode: fullscreen + hide editing chrome (palette/toolbar), read-only feel.
  const rootRef = useRef<HTMLDivElement>(null);
  function togglePresent() {
    const el = rootRef.current;
    if (!document.fullscreenElement && el?.requestFullscreen) { el.requestFullscreen().catch(() => undefined); setPresentMode(true); }
    else { if (document.fullscreenElement) document.exitFullscreen().catch(() => undefined); setPresentMode(false); }
    setTimeout(() => rf.fitView({ padding: 0.15, duration: 400 }), 120);
  }
  useEffect(() => {
    const onFs = () => { if (!document.fullscreenElement) setPresentMode(false); };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  // Categories actually present on the canvas, in catalog order (for the filter menu).
  const presentCategories = useMemo(() => {
    const present = new Set(nodes.map((n) => n.data.category));
    const ordered = (catalog?.categories ?? []).map((c) => c.id).filter((id) => present.has(id));
    for (const id of present) if (!ordered.includes(id)) ordered.push(id);
    return ordered;
  }, [nodes, catalog]);

  // Boundary boxes: group member nodes by resource group or subscription, computing a
  // bounding rectangle around each group's live node positions (real-Azure diagram style).
  const boundaries = useMemo(() => {
    if (boundaryMode === "none") return [] as { key: string; label: string; x: number; y: number; w: number; h: number; tone?: string }[];
    const keyOf = (n: Node<AzData>) => {
      if (boundaryMode === "resource_group") return n.data.resource_group || "";
      if (boundaryMode === "subscription") return n.data.subscription_id || "";
      if (boundaryMode === "region") return n.data.location || "";
      if (boundaryMode === "vnet") return (n.data.meta?.vnet as string) || (isVNet(n.data.type) ? n.data.name : "") || "";
      if (boundaryMode === "subnet") return (n.data.meta?.subnet as string) || "";
      return "";
    };
    const groups = new Map<string, Node<AzData>[]>();
    for (const n of nodes) {
      if (n.data.type === "__note__") continue;
      const k = keyOf(n);
      if (!k) continue;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k)!.push(n);
    }
    const PAD = 26, HEADER = 22, NW = 230, NH = 96;
    const out: { key: string; label: string; x: number; y: number; w: number; h: number; tone?: string }[] = [];
    for (const [k, ns] of groups) {
      if (ns.length < 1) continue;
      const xs = ns.map((n) => n.position.x), ys = ns.map((n) => n.position.y);
      const minX = Math.min(...xs) - PAD, minY = Math.min(...ys) - PAD - HEADER;
      const maxX = Math.max(...xs) + NW + PAD, maxY = Math.max(...ys) + NH + PAD;
      const label = boundaryMode === "subscription" ? (k.length > 14 ? k.slice(0, 8) + "…" : k)
        : boundaryMode === "region" ? friendlyLocation(k)
        : boundaryMode === "vnet" ? `VNet · ${k}`
        : boundaryMode === "subnet" ? `Subnet · ${k}`
        : k;
      const tone = boundaryMode === "vnet" ? "#6366f1" : boundaryMode === "subnet" ? "#0891b2" : boundaryMode === "region" ? "#16a34a" : "#0ea5e9";
      out.push({ key: k, label, x: minX, y: minY, w: maxX - minX, h: maxY - minY, tone });
    }
    return out;
  }, [nodes, boundaryMode]);

  // --- Undo / redo --------------------------------------------------------------------
  // Bulk-delete the current selection (multi-select or single), with history.
  const deleteSelection = useCallback(() => {
    const sel = selIdsRef.current;
    const dn = new Set(sel.nodes.length ? sel.nodes : (selNode ? [selNode] : []));
    const de = new Set(sel.edges.length ? sel.edges : (selEdge ? [selEdge] : []));
    if (!dn.size && !de.size) return;
    pushHistory();
    setNodes((n) => n.filter((x) => !dn.has(x.id)));
    setEdges((ed) => ed.filter((x) => !de.has(x.id) && !dn.has(x.source) && !dn.has(x.target)));
    setSelNode(null); setSelEdge(null); setDirty(true);
  }, [selNode, selEdge, pushHistory]);

  // Keyboard: undo/redo + delete (ignored while typing in a field).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const tag = (el?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || el?.isContentEditable) return;
      const meta = e.ctrlKey || e.metaKey;
      if (meta && e.key.toLowerCase() === "z") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
      if (meta && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
      if (meta && e.key.toLowerCase() === "c") { e.preventDefault(); copySelection(); return; }
      if (meta && e.key.toLowerCase() === "v") { e.preventDefault(); pasteClipboard(); return; }
      if (meta && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelection(); return; }
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelection(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [undo, redo, deleteSelection]);

  // --- Visio tools: align / distribute, copy/paste/duplicate, sticky notes ----------
  // Approx node footprint (measured if available, else a sensible default).
  const nodeSize = useCallback((id: string) => {
    const n = rf.getNode(id);
    return { w: n?.measured?.width ?? 200, h: n?.measured?.height ?? 70 };
  }, [rf]);

  const selectedIds = useCallback((): string[] => {
    const s = selIdsRef.current.nodes;
    if (s.length) return s;
    return selNode ? [selNode] : [];
  }, [selNode]);

  type AlignKind = "left" | "hcenter" | "right" | "top" | "vcenter" | "bottom" | "dist-h" | "dist-v";
  const alignNodes = useCallback((kind: AlignKind) => {
    const ids = selectedIds();
    if (ids.length < 2) return;
    pushHistory();
    setNodes((cur) => {
      const sel = cur.filter((n) => ids.includes(n.id));
      const boxes = sel.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y, ...nodeSize(n.id) }));
      const minX = Math.min(...boxes.map((b) => b.x));
      const maxX = Math.max(...boxes.map((b) => b.x + b.w));
      const minY = Math.min(...boxes.map((b) => b.y));
      const maxY = Math.max(...boxes.map((b) => b.y + b.h));
      const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
      const pos = new Map<string, { x: number; y: number }>();
      if (kind === "dist-h" || kind === "dist-v") {
        const horiz = kind === "dist-h";
        const sorted = [...boxes].sort((a, b) => (horiz ? a.x - b.x : a.y - b.y));
        if (sorted.length >= 3) {
          const first = sorted[0], last = sorted[sorted.length - 1];
          const span = horiz ? (last.x - first.x) : (last.y - first.y);
          const step = span / (sorted.length - 1);
          sorted.forEach((b, i) => {
            pos.set(b.id, horiz ? { x: first.x + step * i, y: b.y } : { x: b.x, y: first.y + step * i });
          });
        }
      } else {
        for (const b of boxes) {
          let x = b.x, y = b.y;
          if (kind === "left") x = minX;
          else if (kind === "right") x = maxX - b.w;
          else if (kind === "hcenter") x = cx - b.w / 2;
          else if (kind === "top") y = minY;
          else if (kind === "bottom") y = maxY - b.h;
          else if (kind === "vcenter") y = cy - b.h / 2;
          pos.set(b.id, { x, y });
        }
      }
      return cur.map((n) => (pos.has(n.id) ? { ...n, position: pos.get(n.id)! } : n));
    });
    setDirty(true);
    setAlignOpen(false);
  }, [selectedIds, pushHistory, nodeSize]);

  // Copy the current selection (nodes + edges fully inside it) to the clipboard.
  const copySelection = useCallback(() => {
    const ids = new Set(selectedIds());
    if (!ids.size) return;
    const s = stateRef.current;
    const nodesOut = s.nodes.filter((n) => ids.has(n.id)).map((n) => ({ ...(n.data as AzData), id: n.id, x: n.position.x, y: n.position.y } as ArchNode));
    const edgesOut = s.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ id: e.id, source: e.source, target: e.target, label: typeof e.label === "string" ? e.label : "", kind: ((e.data?.kind as ArchEdgeKind) ?? "connects_to"), dashed: Boolean(e.data?.dashed) }));
    clipboardRef.current = { nodes: nodesOut, edges: edgesOut };
    setMsg(`Copied ${nodesOut.length} resource(s).`); setTimeout(() => setMsg(""), 1200);
  }, [selectedIds]);

  // Paste the clipboard with fresh ids, offset so it doesn't overlap the originals.
  const pasteClipboard = useCallback(() => {
    const clip = clipboardRef.current;
    if (!clip || !clip.nodes.length) return;
    pushHistory();
    const idMap = new Map<string, string>();
    const OFF = 40;
    const fresh = clip.nodes.map((n) => {
      const nid = newId("n");
      idMap.set(n.id, nid);
      return { ...n, id: nid, x: (n.x || 0) + OFF, y: (n.y || 0) + OFF };
    });
    const a = { ...arch, nodes: fresh, edges: clip.edges.map((e) => ({ ...e, id: newId("e"), source: idMap.get(e.source)!, target: idMap.get(e.target)! })) } as Architecture;
    const newFlowNodes = toFlowNodes(a, colorOf);
    const newFlowEdges = toFlowEdges(a);
    setNodes((cur) => [...cur, ...newFlowNodes]);
    setEdges((cur) => [...cur, ...newFlowEdges]);
    setDirty(true);
  }, [arch, colorOf, pushHistory]);

  // Duplicate = copy + immediate paste.
  const duplicateSelection = useCallback(() => { copySelection(); setTimeout(() => pasteClipboard(), 0); }, [copySelection, pasteClipboard]);

  // Add a sticky-note annotation node at the current viewport center.
  function addNote() {
    pushHistory();
    const center = rf.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 });
    const id = newId("note");
    const node: Node<AzData> = {
      id, type: "azure", position: center,
      data: {
        id, arm_id: "", name: "New note", type: "__note__", category: "other",
        layer: "shared", resource_group: "", subscription_id: "", location: "",
        sku: "", meta: {}, group_id: "", x: center.x, y: center.y, color: "#f59e0b",
      },
    };
    setNodes((n) => [...n, node]);
    setSelNode(id); setSelEdge(null);
    setDirty(true);
  }

  // Insert a canonical Azure starter topology (hub-spoke or AKS baseline) at the viewport
  // center — pre-wired nodes + edges the user can then refine.
  function insertTemplate(kind: "hubspoke" | "aks" | "webapp") {
    pushHistory();
    const base = rf.screenToFlowPosition({ x: window.innerWidth / 2 - 200, y: 160 });
    const mk = (label: string, type: string, category: string, dx: number, dy: number, meta: Record<string, string> = {}): Node<AzData> => {
      const id = newId("n");
      return { id, type: "azure", position: { x: base.x + dx, y: base.y + dy },
        data: { id, arm_id: "", name: label, type, category, layer: layerFor(category), resource_group: "", subscription_id: "", location: "", sku: "", meta, group_id: "", x: base.x + dx, y: base.y + dy, color: colorOf(category) } };
    };
    const link = (s: string, t: string, label: string, ek: ArchEdgeKind = "network"): Edge => ({
      id: newId("e"), source: s, target: t, label, animated: ek === "data_flow",
      markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(ek) },
      style: { stroke: edgeColor(ek), strokeWidth: 1.6 }, data: { kind: ek, dashed: false },
      labelStyle: { fontSize: 10, fill: "#475569" }, labelBgStyle: { fill: "#fff", fillOpacity: 0.85 },
    });
    let nn: Node<AzData>[] = []; let ne: Edge[] = [];
    if (kind === "hubspoke") {
      const fw = mk("Azure Firewall", "microsoft.network/azurefirewalls", "networking", 320, 0);
      const gw = mk("VPN Gateway", "microsoft.network/virtualnetworkgateways", "networking", 0, 120);
      const hub = mk("hub-vnet", "microsoft.network/virtualnetworks", "networking", 320, 120, { vnet: "hub-vnet" });
      const s1 = mk("spoke1-vnet", "microsoft.network/virtualnetworks", "networking", 120, 300, { vnet: "spoke1-vnet" });
      const s2 = mk("spoke2-vnet", "microsoft.network/virtualnetworks", "networking", 540, 300, { vnet: "spoke2-vnet" });
      nn = [fw, gw, hub, s1, s2];
      ne = [link(gw.id, hub.id, "S2S/ER"), link(fw.id, hub.id, "secures"), link(hub.id, s1.id, "peering"), link(hub.id, s2.id, "peering")];
    } else if (kind === "aks") {
      const agw = mk("App Gateway (WAF)", "microsoft.network/applicationgateways", "networking", 0, 0, { sku: "WAF_v2" });
      const aks = mk("AKS cluster", "microsoft.containerservice/managedclusters", "containers", 0, 160);
      const acr = mk("Container Registry", "microsoft.containerregistry/registries", "containers", 320, 160);
      const kv = mk("Key Vault", "microsoft.keyvault/vaults", "security", 320, 320, { publicNetworkAccess: "Disabled" });
      const sql = mk("SQL Database", "microsoft.sql/servers", "data", 0, 320, { publicNetworkAccess: "Disabled" });
      nn = [agw, aks, acr, kv, sql];
      ne = [link(agw.id, aks.id, "HTTPS 443", "data_flow"), link(aks.id, acr.id, "pulls images", "depends_on"), link(aks.id, kv.id, "secrets", "identity"), link(aks.id, sql.id, "SQL 1433", "data_flow")];
    } else {
      const fd = mk("Front Door", "microsoft.network/frontdoors", "networking", 160, 0);
      const app = mk("App Service", "microsoft.web/sites", "web", 160, 160);
      const plan = mk("App Service Plan", "microsoft.web/serverfarms", "web", 420, 160, { sku: "P1v3" });
      const sql = mk("SQL Database", "microsoft.sql/servers", "data", 0, 320, { publicNetworkAccess: "Disabled" });
      const kv = mk("Key Vault", "microsoft.keyvault/vaults", "security", 320, 320, { publicNetworkAccess: "Disabled" });
      nn = [fd, app, plan, sql, kv];
      ne = [link(fd.id, app.id, "HTTPS 443", "data_flow"), link(app.id, plan.id, "hosted on", "depends_on"), link(app.id, sql.id, "SQL 1433", "data_flow"), link(app.id, kv.id, "secrets", "identity")];
    }
    setNodes((cur) => [...cur, ...nn]);
    setEdges((cur) => [...cur, ...ne]);
    setDirty(true);
    setTimeout(() => rf.fitView({ padding: 0.2, duration: 400 }), 80);
  }

  const paletteGroups = useMemo(() => {
    const items = (catalog?.palette ?? []).filter((p) => !paletteQuery || p.label.toLowerCase().includes(paletteQuery.toLowerCase()) || p.type.includes(paletteQuery.toLowerCase()));
    const byCat = new Map<string, ArchitecturePaletteItem[]>();
    for (const p of items) (byCat.get(p.category) ?? byCat.set(p.category, []).get(p.category)!).push(p);
    return Array.from(byCat.entries());
  }, [catalog, paletteQuery]);

  // Toolbar popovers (Filter / Tidy / Export / AI enhance / Ask AI) are mutually exclusive —
  // opening one closes the others so they never overlap and become unusable.
  function togglePanel(target: "filter" | "tidy" | "export" | "ai" | "ask") {
    setFilterOpen((v) => (target === "filter" ? !v : false));
    setTidyOpen((v) => (target === "tidy" ? !v : false));
    setExportOpen((v) => (target === "export" ? !v : false));
    setAiOpen((v) => (target === "ai" ? !v : false));
    setAskOpen((v) => (target === "ask" ? !v : false));
  }
  function closePanels() {
    setFilterOpen(false); setTidyOpen(false); setExportOpen(false); setAiOpen(false); setAskOpen(false);
  }

  function doExport(kind: "svg" | "json" | "mermaid" | "bicep" | "terraform") {
    const model = modelFromState() as Architecture;
    let blob: Blob, ext: string;
    if (kind === "json") { blob = new Blob([JSON.stringify(model, null, 2)], { type: "application/json" }); ext = "json"; }
    else if (kind === "mermaid") { blob = new Blob([toMermaid(model)], { type: "text/plain" }); ext = "mmd"; }
    else if (kind === "bicep") { blob = new Blob([toBicep(model)], { type: "text/plain" }); ext = "bicep"; }
    else if (kind === "terraform") { blob = new Blob([toTerraform(model)], { type: "text/plain" }); ext = "tf"; }
    else { blob = new Blob([toSvg(model, colorOf)], { type: "image/svg+xml" }); ext = "svg"; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${(name || "architecture").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.${ext}`;
    a.click(); URL.revokeObjectURL(url);
    setExportOpen(false);
  }

  // Render the diagram SVG to a PNG and download it (the most-requested format for
  // docs/slides — we already have an SVG renderer, so rasterize it on a canvas).
  function doExportPng() {
    const model = modelFromState() as Architecture;
    const svg = toSvg(model, colorOf);
    const scale = 2; // hi-DPI
    const widthMatch = /width="(\d+)"/.exec(svg);
    const heightMatch = /height="(\d+)"/.exec(svg);
    const w = widthMatch ? Number(widthMatch[1]) : 1200;
    const h = heightMatch ? Number(heightMatch[1]) : 800;
    const img = new Image();
    const svgBlob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(svgBlob);
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = w * scale;
      canvas.height = h * scale;
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.scale(scale, scale);
        ctx.drawImage(img, 0, 0);
        canvas.toBlob((blob) => {
          if (!blob) return;
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `${(name || "architecture").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.png`;
          a.click();
          URL.revokeObjectURL(a.href);
        }, "image/png");
      }
      URL.revokeObjectURL(url);
    };
    img.onerror = () => { URL.revokeObjectURL(url); setMsg("Couldn't render PNG."); };
    img.src = url;
    setExportOpen(false);
  }

  // Import a Mermaid flowchart file → append parsed nodes/edges (undoable).
  const importRef = useRef<HTMLInputElement>(null);
  async function onImportFile(file: File) {
    setMsg("");
    try {
      const text = await file.text();
      const parsed = fromMermaid(text);
      if (!parsed || parsed.nodes.length === 0) { setMsg("Couldn't parse a Mermaid flowchart from that file."); return; }
      pushHistory();
      const merged = { ...arch, nodes: [...(modelFromState().nodes as ArchNode[]), ...parsed.nodes], edges: [...(modelFromState().edges as ArchEdge[]), ...parsed.edges] } as Architecture;
      setNodes(toFlowNodes(merged, colorOf));
      setEdges(toFlowEdges(merged));
      setDirty(true);
      setMsg(`Imported ${parsed.nodes.length} node(s) from Mermaid.`);
      setTimeout(() => { rf.fitView({ padding: 0.2, duration: 400 }); setMsg(""); }, 600);
    } catch (e) { setMsg(formatError(e)); }
  }

  // Phase 6: drift check vs. live Azure Resource Graph.
  async function runDrift() {
    setDriftBusy(true); setMsg("");
    try { setDrift(await api.architectureDrift(arch.id)); }
    catch (e) { setMsg(formatError(e)); }
    finally { setDriftBusy(false); }
  }
  // Phase 7: ask the AI about this architecture.
  async function runAsk() {
    if (!askQ.trim()) return;
    setAskBusy(true); setAskA("");
    try { const r = await api.askArchitecture(arch.id, askQ.trim()); setAskA(r.answer); }
    catch (e) { setAskA(formatError(e)); }
    finally { setAskBusy(false); }
  }


  return (
    <div ref={rootRef} className="flex h-full min-h-0 flex-col bg-white">
      {/* Toolbar */}
      {!presentMode && (
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-3 py-2">
        <input value={name} onFocus={() => pushHistory()} onChange={(e) => { setName(e.target.value); setDirty(true); }}
          className="w-64 rounded-lg border px-2.5 py-1.5 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-brand" />
        {arch.source === "ai" && <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium text-violet-700">✨ AI</span>}
        {dirty && <span className="text-[11px] text-amber-600">● unsaved</span>}

        {/* Find a node */}
        <div className="relative min-w-0">
          <input
            value={search}
            onChange={(e) => { setSearch(e.target.value); setSearchFocus(true); }}
            onFocus={() => setSearchFocus(true)}
            onKeyDown={(e) => { if (e.key === "Enter" && searchMatches[0]) focusNode(searchMatches[0].id); if (e.key === "Escape") { setSearch(""); setSearchFocus(false); } }}
            placeholder="🔍 Find resource…"
            className="w-44 rounded-lg border px-2.5 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
          />
          {searchFocus && searchMatches.length > 0 && (
            <div className="absolute left-0 z-30 mt-1 max-h-60 w-60 overflow-y-auto rounded-lg border bg-white py-1 shadow-xl">
              {searchMatches.map((n) => (
                <button key={n.id} onClick={() => focusNode(n.id)} className="flex w-full items-center gap-2 px-2.5 py-1 text-left hover:bg-gray-50">
                  <AzureIcon kind={n.data.arm_id ? "resource" : "resource_group"} type={n.data.type} className="h-3.5 w-3.5 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[11px] text-gray-700">{n.data.name}</span>
                    <span className="block truncate text-[9px] text-gray-400">{friendlyResourceType(n.data.type)}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex basis-full flex-wrap items-center gap-1.5 border-t border-gray-100 pt-2">
          {/* Undo / redo */}
          <div className="flex overflow-hidden rounded-lg border">
            <button onClick={undo} disabled={!canUndo} title="Undo (Ctrl+Z)" className="px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-30">↶</button>
            <button onClick={redo} disabled={!canRedo} title="Redo (Ctrl+Shift+Z)" className="border-l px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-30">↷</button>
          </div>
          {/* View filters (hide categories) */}
          <div className="relative">
            <button onClick={() => togglePanel("filter")}
              className={`rounded-lg border px-2.5 py-1 text-xs ${hiddenCats.size > 0 ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
              ⛃ Filter{hiddenCats.size > 0 ? ` (${hiddenCats.size})` : ""}
            </button>
            {filterOpen && (
              <div className="absolute right-0 z-20 mt-1 w-56 rounded-lg border bg-white py-1 shadow-lg">
                <div className="flex items-center justify-between px-3 py-1">
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Show categories</span>
                  {hiddenCats.size > 0 && <button onClick={() => setHiddenCats(new Set())} className="text-[10px] text-brand hover:underline">Reset</button>}
                </div>
                {presentCategories.map((cid) => {
                  const shown = !hiddenCats.has(cid);
                  const cat = catalog?.categories.find((c) => c.id === cid);
                  return (
                    <button key={cid} onClick={() => setHiddenCats((s) => { const n = new Set(s); if (n.has(cid)) n.delete(cid); else n.add(cid); return n; })}
                      className="flex w-full items-center gap-2 px-3 py-1 text-left text-xs hover:bg-gray-50">
                      <input type="checkbox" checked={shown} readOnly className="pointer-events-none" />
                      <span className="h-2.5 w-2.5 rounded-sm" style={{ background: colorOf(cid) }} />
                      <span className="flex-1 text-gray-700">{cat?.label ?? cid}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          {/* Impact / blast-radius */}
          <button onClick={() => { setImpactOn((v) => !v); setPathMode(false); }} title="Select a node to highlight everything connected up- and down-stream"
            className={`rounded-lg border px-2.5 py-1 text-xs ${impactOn ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
            ◎ Impact
          </button>
          {/* Ingress path tracing (north-south) */}
          <button onClick={() => { setPathMode((v) => !v); setImpactOn(false); }} title="Select a node to trace the directed downstream (north-south) request path"
            className={`rounded-lg border px-2.5 py-1 text-xs ${pathMode ? "border-emerald-300 bg-emerald-50 text-emerald-700" : "text-gray-600 hover:bg-gray-50"}`}>
            🛣 Path
          </button>
          {/* Boundary containers: pick from a dropdown */}
          <select
            value={boundaryMode}
            onChange={(e) => setBoundaryMode(e.target.value as typeof boundaryMode)}
            title="Group resources into boundary boxes"
            className={`rounded-lg border px-1.5 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-brand ${boundaryMode !== "none" ? "border-sky-300 bg-sky-50 text-sky-700" : "text-gray-600"}`}>
            <option value="none">⬚ No boundaries</option>
            <option value="resource_group">⬚ By Resource Group</option>
            <option value="subscription">⬚ By Subscription</option>
            <option value="vnet">⬚ By VNet</option>
            <option value="subnet">⬚ By Subnet</option>
            <option value="region">⬚ By Region</option>
          </select>
          {/* Azure semantics view: reachability, hosting model, cost */}
          <button onClick={() => setAzureView((v) => !v)} title="Azure view: public/private reachability, PaaS/IaaS tags, peering & private-link edges, indicative cost"
            className={`rounded-lg border px-2.5 py-1 text-xs ${azureView ? "border-indigo-300 bg-indigo-50 text-indigo-700" : "text-gray-600 hover:bg-gray-50"}`}>
            ☁ Azure view
          </button>
          {/* Hosting-model filter */}
          <select value={hostingFilter} onChange={(e) => setHostingFilter(e.target.value)} title="Filter by hosting model"
            className="rounded-lg border px-1.5 py-1 text-xs text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand">
            <option value="">All hosting</option>
            <option value="IaaS">IaaS</option>
            <option value="PaaS">PaaS</option>
            <option value="SaaS">SaaS</option>
            <option value="Net">Network</option>
          </select>
          {/* Well-Architected pillar overlay */}
          {assessReady && assessScore !== null && (
            <select value={pillarOverlay} onChange={(e) => setPillarOverlay(e.target.value)} title="Tint resources failing a Well-Architected pillar"
              className="rounded-lg border px-1.5 py-1 text-xs text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand">
              <option value="">No pillar tint</option>
              <option value="security">🛡 Security</option>
              <option value="reliability">🔄 Reliability</option>
              <option value="cost">💰 Cost</option>
              <option value="operations">⚙ Operations</option>
              <option value="performance">⚡ Performance</option>
            </select>
          )}
          {/* Best-practice lint */}
          <button onClick={() => setLintOpen((v) => !v)}
            className={`rounded-lg border px-2.5 py-1 text-xs ${lintOpen ? "border-amber-300 bg-amber-50 text-amber-700" : "text-gray-600 hover:bg-gray-50"}`}>
            ✓ Review{lint.length > 0 ? ` (${lint.length})` : ""}
          </button>
          {/* Assessment overlay (only when an assessment exists for this workload) */}
          {assessReady && assessScore !== null && (
            <button onClick={() => setAssessOn((v) => !v)} title={`Latest assessment score: ${assessScore}/100 — highlight failing resources`}
              className={`flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs ${assessOn ? "border-red-300 bg-red-50 text-red-700" : "text-gray-600 hover:bg-gray-50"}`}>
              🛡 {assessScore}/100
            </button>
          )}
          {/* Visio tools: snap-to-grid, connector routing, align/distribute, sticky note */}
          <button onClick={() => setSnapEnabled((v) => !v)} title="Snap to grid"
            className={`rounded-lg border px-2.5 py-1 text-xs ${snapEnabled ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
            # Grid
          </button>
          <select value={routing} onChange={(e) => setRouting(e.target.value as typeof routing)} title="Connector routing style"
            className="rounded-lg border px-1.5 py-1 text-xs text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand">
            <option value="bezier">↝ Curved</option>
            <option value="smoothstep">⌐ Orthogonal</option>
            <option value="step">⌐ Right-angle</option>
            <option value="straight">／ Straight</option>
          </select>
          <div className="relative">
            <button onClick={() => setAlignOpen((v) => !v)} title="Align & distribute selected nodes"
              className={`rounded-lg border px-2.5 py-1 text-xs ${alignOpen ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>⊞ Align ▾</button>
            {alignOpen && (
              <div className="absolute right-0 z-20 mt-1 w-52 rounded-lg border bg-white p-2 shadow-lg">
                <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Align (select 2+)</div>
                <div className="grid grid-cols-3 gap-1">
                  <button onClick={() => alignNodes("left")} title="Align left" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">⬅</button>
                  <button onClick={() => alignNodes("hcenter")} title="Center horizontally" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">↔</button>
                  <button onClick={() => alignNodes("right")} title="Align right" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">➡</button>
                  <button onClick={() => alignNodes("top")} title="Align top" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">⬆</button>
                  <button onClick={() => alignNodes("vcenter")} title="Center vertically" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">↕</button>
                  <button onClick={() => alignNodes("bottom")} title="Align bottom" className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">⬇</button>
                </div>
                <div className="mb-1 mt-2 px-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Distribute (select 3+)</div>
                <div className="grid grid-cols-2 gap-1">
                  <button onClick={() => alignNodes("dist-h")} className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">↔ Horizontal</button>
                  <button onClick={() => alignNodes("dist-v")} className="rounded border px-1 py-1.5 text-xs hover:bg-gray-50">↕ Vertical</button>
                </div>
              </div>
            )}
          </div>
          <button onClick={addNote} title="Add a sticky note" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">📝 Note</button>
          <div className="relative">
            <button onClick={() => setTemplateOpen((v) => !v)} title="Insert a canonical Azure topology" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">🏛 Template ▾</button>
            {templateOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setTemplateOpen(false)} />
                <div className="absolute right-0 z-20 mt-1 w-52 rounded-lg border bg-white py-1 shadow-lg">
                  <button onClick={() => { setTemplateOpen(false); insertTemplate("hubspoke"); }} className="block w-full px-3 py-1.5 text-left text-xs text-gray-700 hover:bg-gray-50">🌐 Hub-spoke network</button>
                  <button onClick={() => { setTemplateOpen(false); insertTemplate("aks"); }} className="block w-full px-3 py-1.5 text-left text-xs text-gray-700 hover:bg-gray-50">☸ AKS baseline</button>
                  <button onClick={() => { setTemplateOpen(false); insertTemplate("webapp"); }} className="block w-full px-3 py-1.5 text-left text-xs text-gray-700 hover:bg-gray-50">🌍 Web app (Front Door → App Service → SQL)</button>
                </div>
              </>
            )}
          </div>
          <div className="relative">
            <button onClick={() => togglePanel("tidy")} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">↹ Tidy ▾</button>
            {tidyOpen && (
              <div className="absolute right-0 z-20 mt-1 w-60 rounded-lg border bg-white py-1 shadow-lg">
                <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Auto-layout</div>
                {LAYOUT_OPTIONS.map((o) => (
                  <button key={o.id} onClick={() => applyLayout(o.id)} className="block w-full px-3 py-1.5 text-left hover:bg-gray-50">
                    <div className="flex items-center gap-2 text-xs font-medium text-gray-700"><span className="w-4 shrink-0 text-center text-gray-500">{o.icon}</span>{o.label}</div>
                    <div className="pl-6 text-[10px] text-gray-400">{o.hint}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button onClick={() => togglePanel("ai")} className="rounded-lg border border-brand/40 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/5">✨ AI enhance</button>
          <button onClick={() => togglePanel("ask")} className={`rounded-lg border px-2.5 py-1 text-xs font-medium ${askOpen ? "border-violet-300 bg-violet-50 text-violet-700" : "border-violet-300/60 text-violet-600 hover:bg-violet-50"}`}>💬 Ask AI</button>
          {(arch.workload_id || (arch.nodes || []).some((n) => n.arm_id)) && (
            <button onClick={() => void runDrift()} disabled={driftBusy} title="Compare this diagram against live Azure"
              className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60">{driftBusy ? "Checking…" : "⟳ Drift"}</button>
          )}
          <div className="relative">
            <button onClick={() => togglePanel("export")} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">⬇ Export</button>
            {exportOpen && (
              <div className="absolute right-0 z-20 mt-1 w-44 rounded-lg border bg-white py-1 shadow-lg">
                <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Diagram</div>
                <button onClick={doExportPng} className="block w-full px-3 py-1 text-left text-xs text-gray-600 hover:bg-gray-50">PNG (image)</button>
                {(["svg", "mermaid", "json"] as const).map((k) => (
                  <button key={k} onClick={() => doExport(k)} className="block w-full px-3 py-1 text-left text-xs text-gray-600 hover:bg-gray-50">{k.toUpperCase()}</button>
                ))}
                <div className="mt-1 border-t px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Infrastructure as Code</div>
                <button onClick={() => doExport("bicep")} className="block w-full px-3 py-1 text-left text-xs text-gray-600 hover:bg-gray-50">Bicep (skeleton)</button>
                <button onClick={() => doExport("terraform")} className="block w-full px-3 py-1 text-left text-xs text-gray-600 hover:bg-gray-50">Terraform (skeleton)</button>
              </div>
            )}
          </div>
          <button onClick={() => importRef.current?.click()} title="Import a Mermaid flowchart" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">⬆ Import</button>
          <input ref={importRef} type="file" accept=".mmd,.txt,.md,text/plain" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void onImportFile(f); e.target.value = ""; }} />
          <button onClick={togglePresent} title="Present (fullscreen)" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">⛶ Present</button>
          <button onClick={() => navigate(`/architectures/${arch.id}/memory`)} title="Open this architecture's Memory" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">🧠 Memory</button>
          {arch.workload_id && (
            <button
              onClick={() => {
                try { sessionStorage.setItem("azsup.memoryHandoff", JSON.stringify({ workloadId: arch.workload_id, memoryArchId: arch.id })); } catch { /* ignore */ }
                navigate("/chat");
              }}
              title="Start a deep investigation grounded in this architecture + its memory"
              className="rounded-lg border border-violet-300 bg-violet-50 px-2.5 py-1 text-xs font-medium text-violet-700 hover:bg-violet-100"
            >
              🔎 Investigate
            </button>
          )}
          <button onClick={() => void save()} disabled={saving} className="rounded-lg bg-brand px-3 py-1 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-60">{saving ? "Saving…" : "Save"}</button>
        </div>
      </div>
      )}
      {presentMode && (
        <button onClick={togglePresent} className="absolute right-3 top-3 z-30 rounded-lg border bg-white/90 px-3 py-1.5 text-xs font-medium text-gray-700 shadow hover:bg-white">✕ Exit present</button>
      )}
      {aiOpen && (
        <div className="flex items-center gap-2 border-b bg-violet-50/60 px-3 py-2">
          <span className="text-xs text-violet-700">✨ Refine with AI:</span>
          <input value={aiGoal} onChange={(e) => setAiGoal(e.target.value)} placeholder="e.g. group by tier and add the data flows to the database"
            className="flex-1 rounded-lg border px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
            onKeyDown={(e) => { if (e.key === "Enter") void runEnhance(); }} />
          <button onClick={() => void runEnhance()} disabled={aiBusy || !aiGoal.trim()} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white disabled:opacity-60">{aiBusy ? "Thinking…" : "Apply"}</button>
          <button onClick={() => setAiOpen(false)} className="text-xs text-gray-400 hover:text-gray-600">✕</button>
        </div>
      )}
      {askOpen && !presentMode && (
        <div className="border-b bg-violet-50/40 px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-violet-700">💬 Ask about this architecture:</span>
            <input value={askQ} onChange={(e) => setAskQ(e.target.value)} placeholder="e.g. Where are my single points of failure? Is this zone-redundant?"
              className="flex-1 rounded-lg border px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
              onKeyDown={(e) => { if (e.key === "Enter") void runAsk(); }} />
            <button onClick={() => void runAsk()} disabled={askBusy || !askQ.trim()} className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-60">{askBusy ? "Thinking…" : "Ask"}</button>
            <button onClick={() => { setAskOpen(false); setAskA(""); }} className="text-xs text-gray-400 hover:text-gray-600">✕</button>
          </div>
          {(askBusy || askA) && (
            <div className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-lg border bg-white px-3 py-2 text-xs text-gray-700">
              {askBusy ? "Analyzing the diagram…" : askA}
            </div>
          )}
        </div>
      )}
      {msg && <div className="border-b bg-gray-50 px-3 py-1 text-[11px] text-gray-600">{msg}</div>}

      <div className="flex min-h-0 flex-1">
        {/* Palette */}
        {!presentMode && (
          paletteCollapsed ? (
            <div className="flex w-8 shrink-0 flex-col items-center border-r bg-gray-50/60 py-2">
              <button
                onClick={() => setPaletteCollapsed(false)}
                title="Show resources palette"
                className="rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-700"
              >
                »
              </button>
              <span className="mt-2 select-none text-[10px] font-semibold uppercase tracking-wider text-gray-400 [writing-mode:vertical-rl]">
                Resources
              </span>
            </div>
          ) : (
        <div className="w-52 shrink-0 overflow-y-auto border-r bg-gray-50/60 p-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] font-semibold text-gray-500">Resources — drag to canvas</span>
            <button
              onClick={() => setPaletteCollapsed(true)}
              title="Collapse palette"
              className="rounded p-0.5 text-gray-400 hover:bg-gray-200 hover:text-gray-700"
            >
              «
            </button>
          </div>
          <input value={paletteQuery} onChange={(e) => setPaletteQuery(e.target.value)} placeholder="Filter…" className="mb-1.5 w-full rounded border px-2 py-1 text-[11px]" />
          {paletteGroups.length > 0 && (
            <div className="mb-1.5 flex items-center justify-end gap-2 text-[10px] text-gray-400">
              <button
                onClick={() => setCollapsedCats(new Set())}
                className="hover:text-gray-600 hover:underline"
                title="Expand all categories"
              >
                Expand all
              </button>
              <span className="text-gray-300">·</span>
              <button
                onClick={() => setCollapsedCats(new Set(paletteGroups.map(([c]) => c)))}
                className="hover:text-gray-600 hover:underline"
                title="Collapse all categories"
              >
                Collapse all
              </button>
            </div>
          )}
          {paletteGroups.map(([cat, items]) => {
            // While filtering, always show matches (ignore the collapsed state).
            const collapsed = !paletteQuery && collapsedCats.has(cat);
            return (
            <div key={cat} className="mb-2">
              <button
                onClick={() => setCollapsedCats((prev) => { const n = new Set(prev); if (n.has(cat)) n.delete(cat); else n.add(cat); return n; })}
                className="mb-0.5 flex w-full items-center gap-1 rounded px-0.5 text-[10px] font-medium uppercase text-gray-400 hover:bg-gray-100/70 hover:text-gray-600"
                title={collapsed ? "Expand" : "Collapse"}
              >
                <span className={`text-[9px] transition-transform ${collapsed ? "" : "rotate-90"}`}>▶</span>
                <span className="h-2 w-2 rounded-sm" style={{ background: colorOf(cat) }} />
                <span className="flex-1 truncate text-left">{catalog?.categories.find((c) => c.id === cat)?.label ?? cat}</span>
                <span className="text-gray-300">{items.length}</span>
              </button>
              {!collapsed && items.map((item) => (
                <div key={item.type} draggable
                  onDragStart={(e) => { e.dataTransfer.setData("application/architecture-node", JSON.stringify(item)); e.dataTransfer.effectAllowed = "move"; }}
                  className="mb-0.5 flex cursor-grab items-center gap-1.5 rounded border border-transparent bg-white px-1.5 py-1 text-[11px] text-gray-700 hover:border-gray-200 hover:shadow-sm active:cursor-grabbing">
                  <AzureIcon kind="resource" type={item.type} className="h-3.5 w-3.5" />
                  <span className="truncate">{item.label}</span>
                </div>
              ))}
            </div>
            );
          })}
        </div>
        )
        )}

        {/* Canvas */}
        <div className="relative min-w-0 flex-1" ref={wrapRef} onDrop={onDrop} onDragOver={onDragOver}>
          <ReactFlow
            nodes={displayNodes}
            edges={displayEdges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeDragStart={() => pushHistory()}
            onSelectionChange={(s) => { selIdsRef.current = { nodes: s.nodes.map((n) => n.id), edges: s.edges.map((e) => e.id) }; }}
            deleteKeyCode={null}
            onNodeClick={(_, n) => { setSelNode(n.id); setSelEdge(null); }}
            onEdgeClick={(_, e) => { setSelEdge(e.id); setSelNode(null); }}
            onNodeContextMenu={(e, n) => {
              e.preventDefault();
              if ((n.data as AzData)?.type === "__note__") return;
              const rect = wrapRef.current?.getBoundingClientRect();
              setCtxMenu({ x: e.clientX - (rect?.left ?? 0), y: e.clientY - (rect?.top ?? 0), nodeId: n.id });
            }}
            onPaneClick={() => { setSelNode(null); setSelEdge(null); closePanels(); setSearchFocus(false); setCtxMenu(null); }}
            snapToGrid={snapEnabled}
            snapGrid={[16, 16]}
            fitView
            proOptions={{ hideAttribution: true }}
            minZoom={0.1}
          >
            <BoundaryLayer boxes={boundaries} />
            <Background variant={snapEnabled ? BackgroundVariant.Lines : BackgroundVariant.Dots} gap={snapEnabled ? 16 : 18} size={1} color="#e2e8f0" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor={(n) => ((n.data as AzData)?.color ?? "#cbd5e1")} className="!bg-white" />
          </ReactFlow>

          {/* Right-click context menu (node) */}
          {ctxMenu && (
            <div
              className="absolute z-30 min-w-[160px] rounded-lg border bg-white py-1 text-xs shadow-lg"
              style={{ left: ctxMenu.x, top: ctxMenu.y }}
              onMouseLeave={() => setCtxMenu(null)}
            >
              <button onClick={() => openNetCheck(ctxMenu.nodeId)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50">
                🔌 Test connectivity
              </button>
              <button onClick={() => openDnsDebug(ctxMenu.nodeId)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50">
                🧭 Debug resolution
              </button>
              <button onClick={() => openTeleIntel(ctxMenu.nodeId)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50">
                📈 Analyze telemetry
              </button>
              <button onClick={() => { focusNode(ctxMenu.nodeId); setCtxMenu(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50">
                🎯 Focus node
              </button>
            </div>
          )}

          {/* Azure-view legend */}
          {azureView && !presentMode && (
            <div className="pointer-events-none absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-full border bg-white/95 px-3 py-1 text-[10px] text-gray-600 shadow">
              <span className="mr-2">🌐 public</span>
              <span className="mr-2">🔒 private</span>
              <span className="mr-2"><span className="inline-block h-1.5 w-3 align-middle" style={{ background: "#0d9488" }} /> peering</span>
              <span className="mr-2"><span className="inline-block h-1.5 w-3 align-middle" style={{ background: "#6366f1", borderTop: "1px dashed #6366f1" }} /> private link</span>
              <span><span className="inline-block h-1.5 w-3 align-middle" style={{ background: "#b91c1c" }} /> identity</span>
            </div>
          )}

          {/* Inspector */}
          {(selectedNode || selectedEdge) && (
            <div
              className="absolute z-10 w-60 overflow-y-auto rounded-xl border bg-white p-3 shadow-lg"
              style={inspectorPos ? { left: inspectorPos.left, top: inspectorPos.top, maxHeight: inspectorPos.maxHeight } : { right: 12, top: 12, maxHeight: "calc(100% - 1.5rem)" }}
            >
              {selectedNode && selectedNode.type === "__note__" && (
                <>
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold text-amber-700">📝 Sticky note</span>
                    <button onClick={deleteSel} className="text-[11px] text-red-500 hover:underline">Delete</button>
                  </div>
                  <label className="block"><span className="mb-0.5 block text-[10px] text-gray-500">Text</span>
                    <textarea rows={4} className="w-full resize-y rounded border px-2 py-1 text-xs" value={selectedNode.name}
                      onChange={(e) => updNode(selectedNode.id, { name: e.target.value })} /></label>
                </>
              )}
              {selectedNode && selectedNode.type !== "__note__" && (
                <>
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold text-gray-700">Resource</span>
                    <button onClick={deleteSel} className="text-[11px] text-red-500 hover:underline">Delete</button>
                  </div>
                  <label className="mb-2 block"><span className="mb-0.5 block text-[10px] text-gray-500">Name</span>
                    <input className="w-full rounded border px-2 py-1 text-xs" value={selectedNode.name} onChange={(e) => updNode(selectedNode.id, { name: e.target.value })} /></label>
                  <div className="mb-2"><span className="mb-0.5 block text-[10px] text-gray-500">ARM type</span>
                    <ArmTypeCombobox value={selectedNode.type} palette={catalog?.palette ?? []}
                      onChange={(type, category) => updNode(selectedNode.id, category ? { type, category } : { type })} /></div>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block"><span className="mb-0.5 block text-[10px] text-gray-500">Category</span>
                      <select className="w-full rounded border px-1.5 py-1 text-[11px]" value={selectedNode.category} onChange={(e) => updNode(selectedNode.id, { category: e.target.value })}>
                        {catalog?.categories.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                      </select></label>
                    <label className="block"><span className="mb-0.5 block text-[10px] text-gray-500">Tier</span>
                      <select className="w-full rounded border px-1.5 py-1 text-[11px]" value={selectedNode.layer} onChange={(e) => updNode(selectedNode.id, { layer: e.target.value })}>
                        {LAYER_ORDER.map((l) => <option key={l} value={l}>{l}</option>)}
                      </select></label>
                  </div>
                  {selectedNode.arm_id && <div className="mt-2 truncate text-[9px] text-gray-400" title={selectedNode.arm_id}>{selectedNode.arm_id}</div>}
                  {selectedNode.arm_id && selectedNode.arm_id.startsWith("/subscriptions/") && (
                    <a href={`https://portal.azure.com/#@/resource${selectedNode.arm_id}/overview`} target="_blank" rel="noreferrer"
                      className="mt-1 inline-flex items-center gap-1 text-[10px] text-blue-600 hover:underline">
                      Open in Azure portal
                      <svg className="h-2.5 w-2.5 opacity-70" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 3h7v7M21 3l-9 9M5 5h6M5 5v14h14v-6" strokeLinecap="round" strokeLinejoin="round" /></svg>
                    </a>
                  )}
                  <button
                    onClick={() => openNetCheck(selectedNode.id)}
                    className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-teal-300 bg-teal-50 px-2 py-1.5 text-[11px] font-medium text-teal-700 hover:bg-teal-100"
                  >
                    🔌 Test connectivity
                  </button>
                  <button
                    onClick={() => openDnsDebug(selectedNode.id)}
                    className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-lg border border-sky-300 bg-sky-50 px-2 py-1.5 text-[11px] font-medium text-sky-700 hover:bg-sky-100"
                  >
                    🧭 Debug resolution
                  </button>
                  {Object.keys(selectedNode.meta || {}).length > 0 && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-[10px] font-medium text-gray-500">Properties ({Object.keys(selectedNode.meta).length})</summary>
                      <div className="mt-1 max-h-40 space-y-0.5 overflow-y-auto rounded border bg-gray-50 p-1.5">
                        {Object.entries(selectedNode.meta).map(([k, v]) => (
                          <div key={k} className="flex justify-between gap-2 text-[9px]"><span className="shrink-0 text-gray-400">{k}</span><span className="truncate text-gray-700" title={String(v)}>{String(v)}</span></div>
                        ))}
                      </div>
                    </details>
                  )}
                  {(() => {
                    const af = selectedNode.arm_id ? assessMap.get(selectedNode.arm_id.toLowerCase()) : undefined;
                    const findings = af?.findings ?? [];
                    if (findings.length === 0) return null;
                    const sevCls: Record<string, string> = { critical: "bg-red-500", error: "bg-orange-500", warning: "bg-amber-500", info: "bg-sky-500" };
                    return (
                      <details className="mt-2" open>
                        <summary className="cursor-pointer text-[10px] font-medium text-red-600">🛡 Assessment findings ({findings.length})</summary>
                        <div className="mt-1 max-h-40 space-y-1 overflow-y-auto rounded border border-red-100 bg-red-50/40 p-1.5">
                          {findings.map((f, i) => (
                            <div key={i} className="flex items-start gap-1.5 text-[10px]">
                              <span className={`mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full ${sevCls[f.severity] ?? "bg-gray-400"}`} />
                              <span className="min-w-0 text-gray-700" title={f.description || f.title}>{f.title}</span>
                            </div>
                          ))}
                        </div>
                      </details>
                    );
                  })()}
                </>
              )}
              {selectedEdge && (
                <>
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold text-gray-700">Connection</span>
                    <button onClick={deleteSel} className="text-[11px] text-red-500 hover:underline">Delete</button>
                  </div>
                  <label className="mb-2 block"><span className="mb-0.5 block text-[10px] text-gray-500">Label</span>
                    <input className="w-full rounded border px-2 py-1 text-xs" value={typeof selectedEdge.label === "string" ? selectedEdge.label : ""} onChange={(e) => updEdge(selectedEdge.id, { label: e.target.value })} /></label>
                  <label className="mb-2 block"><span className="mb-0.5 block text-[10px] text-gray-500">Kind</span>
                    <select className="w-full rounded border px-1.5 py-1 text-[11px]" value={(selectedEdge.data?.kind as string) ?? "connects_to"} onChange={(e) => updEdge(selectedEdge.id, { kind: e.target.value as ArchEdgeKind })}>
                      {EDGE_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                    </select></label>
                  <label className="flex items-center gap-1.5 text-[11px] text-gray-600">
                    <input type="checkbox" checked={Boolean(selectedEdge.data?.dashed)} onChange={(e) => updEdge(selectedEdge.id, { dashed: e.target.checked })} /> Dashed (logical)
                  </label>
                </>
              )}
            </div>
          )}

          {/* Best-practice review panel */}
          {lintOpen && (
            <div className="absolute bottom-3 left-3 z-10 max-h-[60%] w-80 overflow-hidden rounded-xl border bg-white shadow-xl">
              <div className="flex items-center justify-between border-b px-3 py-2">
                <span className="text-xs font-semibold text-gray-700">✓ Best-practice review</span>
                <button onClick={() => setLintOpen(false)} className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>
              </div>
              <div className="max-h-80 space-y-1.5 overflow-y-auto p-2">
                {lint.length === 0 && <div className="rounded-lg border border-dashed p-4 text-center text-xs text-green-600">✓ No issues found — looks well-architected.</div>}
                {lint.map((f) => (
                  <button key={f.id} onClick={() => f.nodeId && focusNode(f.nodeId)}
                    className={`block w-full rounded-lg border px-2.5 py-1.5 text-left ${f.nodeId ? "hover:bg-gray-50" : ""}`}>
                    <div className="flex items-center gap-1.5">
                      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${f.severity === "warning" ? "bg-amber-500" : "bg-sky-500"}`} />
                      <span className="text-[11px] font-medium text-gray-700">{f.title}</span>
                      <span className="ml-auto text-[9px] uppercase text-gray-400">{f.severity}</span>
                    </div>
                    <p className="mt-0.5 pl-3.5 text-[10px] text-gray-500">{f.detail}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Drift report */}
          {drift && (
            <div className="absolute bottom-3 right-3 z-10 max-h-[60%] w-80 overflow-hidden rounded-xl border bg-white shadow-xl">
              <div className="flex items-center justify-between border-b px-3 py-2">
                <span className="text-xs font-semibold text-gray-700">⟳ Drift vs. live Azure</span>
                <button onClick={() => setDrift(null)} className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>
              </div>
              <div className="border-b px-3 py-1.5 text-[11px] text-gray-500">
                {drift.matched} of {drift.diagram_count} diagram resources match · {drift.live_count} live in Azure
              </div>
              <div className="max-h-72 space-y-2 overflow-y-auto p-2">
                {drift.in_sync && <div className="rounded-lg border border-dashed p-4 text-center text-xs text-green-600">✓ In sync — the diagram matches Azure.</div>}
                {drift.removed.length > 0 && (
                  <div>
                    <div className="mb-1 text-[10px] font-semibold uppercase text-red-500">Gone from Azure ({drift.removed.length})</div>
                    {drift.removed.map((r) => (
                      <div key={r.id} className="rounded-lg border border-red-200 bg-red-50/50 px-2 py-1">
                        <div className="text-[11px] font-medium text-red-700">{r.name}</div>
                        <div className="truncate text-[9px] text-gray-400">{friendlyResourceType(r.type)}</div>
                      </div>
                    ))}
                  </div>
                )}
                {drift.added.length > 0 && (
                  <div>
                    <div className="mb-1 text-[10px] font-semibold uppercase text-amber-600">New in Azure ({drift.added.length})</div>
                    {drift.added.map((r) => (
                      <div key={r.arm_id} className="rounded-lg border border-amber-200 bg-amber-50/50 px-2 py-1">
                        <div className="text-[11px] font-medium text-amber-700">{r.name}</div>
                        <div className="truncate text-[9px] text-gray-400">{friendlyResourceType(r.type)} · {r.resource_group}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
      {netCheck && (
        <NetCheckModal
          architectureId={arch.id}
          preset={{ targetNodeId: netCheck.targetNodeId, targetHost: netCheck.targetHost }}
          onClose={() => setNetCheck(null)}
        />
      )}
      {dnsDebug && (
        <DnsDebugModal
          architectureId={arch.id}
          preset={{ fqdn: dnsDebug.fqdn }}
          onClose={() => setDnsDebug(null)}
        />
      )}
    </div>
  );
}

// --- standalone exporters (no deps) ----------------------------------------
function toMermaid(a: Architecture): string {
  const safe = (s: string) => (s || "").replace(/["\n]/g, " ").slice(0, 60);
  const idmap = new Map(a.nodes.map((n, i) => [n.id, `N${i}`]));
  const lines = ["flowchart TD"];
  for (const n of a.nodes) lines.push(`  ${idmap.get(n.id)}["${safe(n.name)}<br/><small>${safe(friendlyish(n.type))}</small>"]`);
  for (const e of a.edges) {
    const s = idmap.get(e.source), t = idmap.get(e.target);
    if (!s || !t) continue;
    lines.push(e.label ? `  ${s} -->|${safe(e.label)}| ${t}` : `  ${s} --> ${t}`);
  }
  return lines.join("\n");
}
function friendlyish(t: string): string {
  return (t || "").split("/").pop() || t;
}
function toSvg(a: Architecture, colorOf: (c: string) => string): string {
  const W = 240, H = 56;
  const xs = a.nodes.map((n) => n.x || 0), ys = a.nodes.map((n) => n.y || 0);
  const maxX = Math.max(0, ...xs) + W + 60, maxY = Math.max(0, ...ys) + H + 60;
  const pos = new Map(a.nodes.map((n) => [n.id, { x: (n.x || 0) + 60, y: (n.y || 0) + 40 }]));
  const esc = (s: string) => (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const edgesSvg = a.edges.map((e) => {
    const s = pos.get(e.source), t = pos.get(e.target);
    if (!s || !t) return "";
    const x1 = s.x + W / 2, y1 = s.y + H / 2, x2 = t.x + W / 2, y2 = t.y + H / 2;
    const col = edgeColor(e.kind);
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${col}" stroke-width="1.5" ${e.dashed ? 'stroke-dasharray="5 4"' : ""} marker-end="url(#arrow)"/>` +
      (e.label ? `<text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 3}" font-size="10" fill="#475569" text-anchor="middle">${esc(e.label)}</text>` : "");
  }).join("");
  const nodesSvg = a.nodes.map((n) => {
    const p = pos.get(n.id)!; const col = colorOf(n.category);
    return `<g transform="translate(${p.x},${p.y})">` +
      `<rect width="${W}" height="${H}" rx="10" fill="#fff" stroke="#e2e8f0"/>` +
      `<rect width="${W}" height="3" rx="1.5" fill="${col}"/>` +
      `<text x="12" y="24" font-size="12" font-weight="600" fill="#1f2937">${esc(n.name.slice(0, 28))}</text>` +
      `<text x="12" y="40" font-size="10" fill="#94a3b8">${esc(friendlyish(n.type))}</text></g>`;
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${maxX}" height="${maxY}" viewBox="0 0 ${maxX} ${maxY}" font-family="system-ui,Segoe UI,sans-serif">` +
    `<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#64748b"/></marker></defs>` +
    `<rect width="${maxX}" height="${maxY}" fill="#f8fafc"/>${edgesSvg}${nodesSvg}</svg>`;
}

// --- IaC skeleton exporters (Bicep / Terraform) ----------------------------
const _bicepId = (s: string) => {
  const id = (s || "res").replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, "r$1");
  return id || "res";
};
// ARM type -> {bicep symbolic resource type@api, terraform type}. Only the most common
// types get a real mapping; unmapped types fall back to a generic ARM resource block.
const IAC_MAP: Record<string, { arm: string; tf: string }> = {
  "microsoft.web/sites": { arm: "Microsoft.Web/sites@2023-12-01", tf: "azurerm_linux_web_app" },
  "microsoft.web/serverfarms": { arm: "Microsoft.Web/serverfarms@2023-12-01", tf: "azurerm_service_plan" },
  "microsoft.web/staticsites": { arm: "Microsoft.Web/staticSites@2023-12-01", tf: "azurerm_static_web_app" },
  "microsoft.storage/storageaccounts": { arm: "Microsoft.Storage/storageAccounts@2023-05-01", tf: "azurerm_storage_account" },
  "microsoft.sql/servers": { arm: "Microsoft.Sql/servers@2023-08-01-preview", tf: "azurerm_mssql_server" },
  "microsoft.sql/servers/databases": { arm: "Microsoft.Sql/servers/databases@2023-08-01-preview", tf: "azurerm_mssql_database" },
  "microsoft.keyvault/vaults": { arm: "Microsoft.KeyVault/vaults@2023-07-01", tf: "azurerm_key_vault" },
  "microsoft.compute/virtualmachines": { arm: "Microsoft.Compute/virtualMachines@2024-03-01", tf: "azurerm_linux_virtual_machine" },
  "microsoft.network/virtualnetworks": { arm: "Microsoft.Network/virtualNetworks@2023-11-01", tf: "azurerm_virtual_network" },
  "microsoft.network/applicationgateways": { arm: "Microsoft.Network/applicationGateways@2023-11-01", tf: "azurerm_application_gateway" },
  "microsoft.network/loadbalancers": { arm: "Microsoft.Network/loadBalancers@2023-11-01", tf: "azurerm_lb" },
  "microsoft.network/publicipaddresses": { arm: "Microsoft.Network/publicIPAddresses@2023-11-01", tf: "azurerm_public_ip" },
  "microsoft.containerservice/managedclusters": { arm: "Microsoft.ContainerService/managedClusters@2024-02-01", tf: "azurerm_kubernetes_cluster" },
  "microsoft.containerregistry/registries": { arm: "Microsoft.ContainerRegistry/registries@2023-07-01", tf: "azurerm_container_registry" },
  "microsoft.documentdb/databaseaccounts": { arm: "Microsoft.DocumentDB/databaseAccounts@2024-05-15", tf: "azurerm_cosmosdb_account" },
  "microsoft.cache/redis": { arm: "Microsoft.Cache/redis@2024-03-01", tf: "azurerm_redis_cache" },
  "microsoft.servicebus/namespaces": { arm: "Microsoft.ServiceBus/namespaces@2022-10-01-preview", tf: "azurerm_servicebus_namespace" },
  "microsoft.eventhub/namespaces": { arm: "Microsoft.EventHub/namespaces@2024-01-01", tf: "azurerm_eventhub_namespace" },
  "microsoft.insights/components": { arm: "Microsoft.Insights/components@2020-02-02", tf: "azurerm_application_insights" },
  "microsoft.operationalinsights/workspaces": { arm: "Microsoft.OperationalInsights/workspaces@2023-09-01", tf: "azurerm_log_analytics_workspace" },
  "microsoft.recoveryservices/vaults": { arm: "Microsoft.RecoveryServices/vaults@2024-04-01", tf: "azurerm_recovery_services_vault" },
  "microsoft.cognitiveservices/accounts": { arm: "Microsoft.CognitiveServices/accounts@2024-10-01", tf: "azurerm_cognitive_account" },
  "microsoft.datafactory/factories": { arm: "Microsoft.DataFactory/factories@2018-06-01", tf: "azurerm_data_factory" },
  "microsoft.apimanagement/service": { arm: "Microsoft.ApiManagement/service@2023-05-01-preview", tf: "azurerm_api_management" },
  "microsoft.logic/workflows": { arm: "Microsoft.Logic/workflows@2019-05-01", tf: "azurerm_logic_app_workflow" },
};

function toBicep(a: Architecture): string {
  const lines = [
    "// Bicep skeleton generated from architecture diagram — review & complete before deploy.",
    `// Architecture: ${(a.name || "architecture").replace(/\n/g, " ")}`,
    "",
    "@description('Azure region for all resources.')",
    "param location string = resourceGroup().location",
    "",
  ];
  const used = new Set<string>();
  for (const n of a.nodes) {
    const t = (n.type || "").toLowerCase();
    const map = IAC_MAP[t];
    let sym = _bicepId(n.name || t.split("/").pop() || "res");
    while (used.has(sym)) sym += "_";
    used.add(sym);
    if (map) {
      lines.push(`resource ${sym} '${map.arm}' = {`);
      lines.push(`  name: '${(n.name || sym).slice(0, 60)}'`);
      lines.push("  location: location");
      if (n.sku) lines.push(`  sku: { name: '${n.sku}' }`);
      lines.push("  properties: {}");
      lines.push("}");
    } else {
      lines.push(`// TODO: unmapped type '${n.type}' — add a resource for '${n.name}'.`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

function toTerraform(a: Architecture): string {
  const lines = [
    "# Terraform skeleton generated from architecture diagram — review & complete before apply.",
    `# Architecture: ${(a.name || "architecture").replace(/\n/g, " ")}`,
    "",
    'terraform {',
    '  required_providers { azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" } }',
    "}",
    'provider "azurerm" { features {} }',
    "",
    'variable "location" { type = string, default = "eastus" }',
    'variable "resource_group_name" { type = string }',
    "",
  ];
  const used = new Set<string>();
  for (const n of a.nodes) {
    const t = (n.type || "").toLowerCase();
    const map = IAC_MAP[t];
    let sym = _bicepId(n.name || t.split("/").pop() || "res").toLowerCase();
    while (used.has(sym)) sym += "_";
    used.add(sym);
    if (map) {
      lines.push(`resource "${map.tf}" "${sym}" {`);
      lines.push(`  name                = "${(n.name || sym).slice(0, 60)}"`);
      lines.push("  location            = var.location");
      lines.push("  resource_group_name = var.resource_group_name");
      lines.push("  # TODO: required arguments for this resource type");
      lines.push("}");
    } else {
      lines.push(`# TODO: unmapped type '${n.type}' — add a resource for '${n.name}'.`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

// --- Mermaid flowchart importer --------------------------------------------
// Parses simple `flowchart`/`graph` Mermaid: `A["Label"]` nodes and `A --> B`,
// `A -->|label| B` edges. Returns nodes (auto-laid-out) + edges, or null on failure.
function fromMermaid(text: string): { nodes: ArchNode[]; edges: ArchEdge[] } | null {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!lines.some((l) => /^(flowchart|graph)\b/i.test(l))) return null;
  const labels = new Map<string, string>();
  const order: string[] = [];
  const ensure = (id: string, label?: string) => {
    if (!labels.has(id)) { labels.set(id, label ?? id); order.push(id); }
    else if (label) labels.set(id, label);
  };
  const edges: { s: string; t: string; label: string }[] = [];
  const nodeDecl = /([A-Za-z0-9_]+)\s*[[({"]+([^\]})"]+)[\]})"]+/g;
  const edgeRe = /([A-Za-z0-9_]+)\s*(?:--?>?|===?>?|-\.->|--x|--o)\s*(?:\|([^|]*)\|)?\s*([A-Za-z0-9_]+)/;
  for (const line of lines) {
    if (/^(flowchart|graph|subgraph|end|classDef|class|style|%%)/i.test(line)) {
      // still capture inline node decls on subgraph lines
    }
    const em = line.match(edgeRe);
    if (em) {
      const [, s, lab, t] = em;
      // capture labels if the node decls are inline on the edge line
      let m: RegExpExecArray | null;
      nodeDecl.lastIndex = 0;
      while ((m = nodeDecl.exec(line))) ensure(m[1], m[2].replace(/<br\/?>/g, " ").replace(/<\/?small>/g, "").trim());
      ensure(s); ensure(t);
      edges.push({ s, t, label: (lab || "").trim() });
      continue;
    }
    let m: RegExpExecArray | null;
    nodeDecl.lastIndex = 0;
    while ((m = nodeDecl.exec(line))) ensure(m[1], m[2].replace(/<br\/?>/g, " ").replace(/<\/?small>/g, "").trim());
  }
  if (order.length === 0) return null;
  const cols = Math.max(1, Math.ceil(Math.sqrt(order.length)));
  const idMap = new Map<string, string>();
  const nodes: ArchNode[] = order.map((mid, i) => {
    const nid = newId("n");
    idMap.set(mid, nid);
    return {
      id: nid, arm_id: "", name: labels.get(mid) || mid, type: "", category: "other", layer: "shared",
      resource_group: "", subscription_id: "", location: "", sku: "", meta: {}, group_id: "",
      x: (i % cols) * 250, y: Math.floor(i / cols) * 135,
    } as ArchNode;
  });
  const archEdges: ArchEdge[] = edges
    .filter((e) => idMap.has(e.s) && idMap.has(e.t))
    .map((e) => ({ id: newId("e"), source: idMap.get(e.s)!, target: idMap.get(e.t)!, label: e.label, kind: "connects_to" as ArchEdgeKind, dashed: false }));
  return { nodes, edges: archEdges };
}

export function ArchitectureCanvas({ arch, onSaved }: { arch: Architecture; onSaved: (a: Architecture) => void }) {
  const [catalog, setCatalog] = useState<ArchitectureCatalog>();
  useEffect(() => { api.architectureCatalog().then(setCatalog).catch(() => undefined); }, []);
  return (
    <ReactFlowProvider>
      <CanvasInner arch={arch} catalog={catalog} onSaved={onSaved} />
    </ReactFlowProvider>
  );
}

// --- read-only preview (view a past revision without restoring) -------------
function PreviewInner({ arch, catalog }: { arch: Architecture; catalog: ArchitectureCatalog | undefined }) {
  const colorOf = useCallback(
    (cat: string) => catalog?.categories.find((c) => c.id === cat)?.color ?? "#6b7280",
    [catalog],
  );
  const nodes = useMemo(() => toFlowNodes(arch, colorOf), [arch, colorOf]);
  const edges = useMemo(() => toFlowEdges(arch), [arch]);
  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      fitView
      proOptions={{ hideAttribution: true }}
      minZoom={0.1}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#e2e8f0" />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable nodeColor={(n) => ((n.data as AzData)?.color ?? "#cbd5e1")} className="!bg-white" />
    </ReactFlow>
  );
}

/** A non-editable rendering of an architecture (used to preview a past revision). */
export function ArchitecturePreview({ arch }: { arch: Architecture }) {
  const [catalog, setCatalog] = useState<ArchitectureCatalog>();
  useEffect(() => { api.architectureCatalog().then(setCatalog).catch(() => undefined); }, []);
  return (
    <ReactFlowProvider>
      <PreviewInner arch={arch} catalog={catalog} />
    </ReactFlowProvider>
  );
}
