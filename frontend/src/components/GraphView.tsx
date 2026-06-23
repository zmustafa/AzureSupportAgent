import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import {
  api,
  type GraphEdge,
  type GraphNode,
  type GraphNodeDetail,
  type GraphNodeKind,
  type GraphResult,
  type GraphView,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import {
  ALL_KINDS,
  KIND_META,
  LENSES,
  type Lens,
  buildStylesheet,
  defaultRing,
  haloColor,
  lensColor,
  toElements,
} from "./graph/graphStyle";
import { GraphInspector } from "./graph/GraphInspector";
import { AnalyticsPanel, AskPanel, Minimap, ViewsPanel, ZoomControl } from "./graph/GraphPanels";
import { kindIconUri } from "./graph/graphIcons";

// Register the fcose layout once (far better node separation than the built-in cose for
// our dense estate graphs). Guarded so HMR re-imports don't double-register.
let _fcoseRegistered = false;
if (!_fcoseRegistered) {
  try { cytoscape.use(fcose as unknown as cytoscape.Ext); _fcoseRegistered = true; } catch { /* already registered */ }
}

type Core = cytoscape.Core;
type EventObject = cytoscape.EventObject;

type Mode = "explore" | "path" | "blast";
type LeftPanel = "none" | "analytics" | "ask" | "views";
type CtxMenu = { x: number; y: number; nodeId?: string; kind?: GraphNodeKind } | null;
type QuickCard = { x: number; y: number; node: GraphNode } | null;

const OVERLAY_OPTS: { id: string; label: string }[] = [
  { id: "cost", label: "Cost" },
  { id: "coverage", label: "Coverage" },
  { id: "retirement", label: "Retirements" },
  { id: "rbac", label: "Access" },
  { id: "change", label: "Changes" },
];

// A full, restorable snapshot of the canvas for the undo/redo history: every element (data +
// position + classes), the camera, and the expand/collapse bookkeeping. nodeDataRef is kept
// cumulative (never shrinks) so it isn't duplicated into each snapshot.
type GraphSnapshot = {
  elements: any[];
  pan: { x: number; y: number };
  zoom: number;
  expanded: [string, string[]][];
  collapsed: [string, { nodes: GraphNode[]; edges: GraphEdge[] }][];
  focusScope: { kind: string; id?: string; ids?: string[] } | null;
  stats: { nodes: number; edges: number };
};

export function GraphPanel() {
  const navigate = useNavigate();
  const { focusId } = useParams<{ focusId?: string }>();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const nodeDataRef = useRef<Map<string, GraphNode>>(new Map());
  const collapsedRef = useRef<Map<string, { nodes: GraphNode[]; edges: GraphEdge[] }>>(new Map());
  // Which nodes are currently expanded, and the child node ids each expansion ADDED — so a
  // second double-click collapses (removes) exactly what the first one added.
  const expandedRef = useRef<Map<string, string[]>>(new Map());
  // Undo/redo: a stack of full graph snapshots. A debounced "add/remove" listener commits a
  // snapshot after each structural change (expand, collapse, show-findings, focus…); undo/redo
  // restore a prior/next snapshot. restoringRef guards the listener while we rebuild the canvas.
  const historyRef = useRef<GraphSnapshot[]>([]);
  const historyPosRef = useRef(-1);
  const [histState, setHistState] = useState({ canUndo: false, canRedo: false });

  const [connectionId, setConnectionId] = usePersistedState<string>("azsup.graph.connection", "");
  const [lens, setLens] = usePersistedState<Lens>("azsup.graph.lens", "none");
  // Mirror of `lens` for use inside stable useCallbacks (avoids stale closures + dep churn).
  const lensRef = useRef(lens);
  lensRef.current = lens;
  const [hidden, setHidden] = useState<Set<GraphNodeKind>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [ctx, setCtx] = useState<CtxMenu>(null);
  const [quickCard, setQuickCard] = useState<QuickCard>(null);
  // Measured heights of the floating overlays, so we can clamp them inside the canvas (their
  // height is variable: the menu has a different item count for node vs canvas, the card varies
  // by node). Ref callbacks update these on mount; the render clamps top against the container.
  const [ctxMenuH, setCtxMenuH] = useState(280);
  const ctxMenuRef = useCallback((el: HTMLDivElement | null) => { if (el) setCtxMenuH(el.offsetHeight); }, []);
  const [cardH, setCardH] = useState(56);
  const cardRef = useCallback((el: HTMLDivElement | null) => { if (el) setCardH(el.offsetHeight); }, []);
  const [searchTerm, setSearchTerm] = useState("");
  const [searchResults, setSearchResults] = useState<GraphNode[]>([]);
  const [stats, setStats] = useState<{ nodes: number; edges: number }>({ nodes: 0, edges: 0 });
  const [status, setStatus] = useState<string>("");
  const [expanding, setExpanding] = useState(false);
  const [mode, setMode] = useState<Mode>("explore");
  const [pathSource, setPathSource] = useState<string | null>(null);
  const [leftPanel, setLeftPanel] = useState<LeftPanel>("none");
  const [overlays, setOverlays] = useState<Set<string>>(new Set());
  const [driftMode, setDriftMode] = useState(false);
  const [focusScope, setFocusScope] = useState<{ kind: string; id?: string; ids?: string[] } | null>(null);
  // Mirror of focusScope so the deferred history snapshot reads the current scope (not a stale
  // closure) when it captures ~360ms after a focus/back navigation.
  const focusScopeRef = useRef(focusScope);
  useEffect(() => { focusScopeRef.current = focusScope; }, [focusScope]);
  const [selectedWls, setSelectedWls] = useState<Set<string>>(new Set());
  const [wlFilter, setWlFilter] = useState("");
  const [tourStep, setTourStep] = useState(-1);
  const [dark, setDark] = usePersistedState<boolean>("azsup.graph.dark", false);
  const [railOpen, setRailOpen] = usePersistedState<boolean>("azsup.graph.rail", true);
  const [viewMenu, setViewMenu] = useState(false);
  const [cyReady, setCyReady] = useState(false);
  // The graph "view" (layout). Defaults to Organic; remembered server-side per Azure tenant.
  // A ref mirrors it so the (one-shot) overview-load effects always read the latest value
  // without re-subscribing.
  type GLayout = "organic" | "hierarchy" | "concentric";
  const [layout, setLayout] = useState<GLayout>("organic");
  const layoutRef = useRef<GLayout>("organic");

  const connQ = useQuery({ queryKey: ["azure-connections"], queryFn: api.azureConnections, staleTime: 60_000 });
  const effectiveConn = useMemo(() => {
    const conns = connQ.data?.connections || [];
    if (connectionId && conns.some((c) => c.id === connectionId)) return connectionId;
    const def = conns.find((c) => c.is_default) || conns[0];
    return def?.id || "";
  }, [connQ.data, connectionId]);
  // Tenant of the selected connection — used to deep-link into the right Azure Portal directory
  // AND to remember the chosen graph view per tenant.
  const effectiveTenant = useMemo(
    () => (connQ.data?.connections || []).find((c) => c.id === effectiveConn)?.tenant_id || "",
    [connQ.data, effectiveConn],
  );

  // Server-side remembered layout for the selected tenant.
  const prefsQ = useQuery({
    queryKey: ["graph-prefs", effectiveTenant],
    queryFn: () => api.graphPrefs(effectiveTenant),
    enabled: !!connQ.data,
    staleTime: 30_000,
  });

  const overviewQ = useQuery({
    queryKey: ["graph-overview", effectiveConn],
    queryFn: () => api.graphOverview(effectiveConn),
    staleTime: 30_000,
  });

  const detailQ = useQuery<GraphNodeDetail>({
    queryKey: ["graph-node", selected, effectiveConn],
    queryFn: () => api.graphNode(selected!, effectiveConn),
    enabled: !!selected,
    staleTime: 15_000,
  });

  // -------------------------------------------------- helpers
  const remember = useCallback((nodes: GraphNode[]) => {
    for (const n of nodes) nodeDataRef.current.set(n.id, n);
  }, []);

  const currentElements = useCallback((): { nodes: GraphNode[]; edges: GraphEdge[] } => {
    const cy = cyRef.current;
    if (!cy) return { nodes: [], edges: [] };
    const nodes: GraphNode[] = cy.nodes().map((n) => nodeDataRef.current.get(n.id()) || ({ id: n.id(), kind: n.data("kind"), label: n.data("label"), data: {}, badges: {}, expandable: false } as GraphNode));
    const edges: GraphEdge[] = cy.edges().map((e) => ({ id: e.id(), source: e.source().id(), target: e.target().id(), kind: e.data("kind"), label: "" }));
    return { nodes, edges };
  }, []);

  const applyHidden = useCallback((set: Set<GraphNodeKind>) => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().forEach((n) => {
      n.style("display", set.has(n.data("kind") as GraphNodeKind) ? "none" : "element");
    });
  }, []);

  const applyLens = useCallback((l: Lens) => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const full = nodeDataRef.current.get(n.id());
        const lc = full ? lensColor(l, full) : "";
        const ring = lc || (full ? defaultRing(full) : KIND_META[n.data("kind") as GraphNodeKind]?.color || "#94a3b8");
        n.data("ring", ring);
        const halo = full ? haloColor(l, full) : "";
        if (halo) n.data("halo", halo); else n.removeData("halo");
      });
    });
  }, []);

  const runLayout = useCallback((name: string) => {
    const cy = cyRef.current;
    if (!cy || cy.elements().length === 0) return;
    // "organic" maps to fcose (best separation); "hierarchy" to breadthfirst; "radial" to concentric.
    const real = name === "organic" ? "fcose" : name === "hierarchy" ? "breadthfirst" : name;
    // animate:false → nodes snap to final positions immediately, so the subsequent fit always
    // frames the real bounds (animated layouts race the fit and leave the canvas blank).
    const opts: any = { name: real, animate: false, fit: true, padding: 50 };
    if (real === "breadthfirst") {
      opts.directed = true;
      opts.spacingFactor = 1.15;
      const roots = cy.nodes('[kind = "tenant_connection"]');
      if (roots.nonempty()) opts.roots = roots;
    } else if (real === "fcose") {
      opts.quality = "default";
      opts.nodeRepulsion = 9000;
      opts.idealEdgeLength = (edge: any) => (edge.data("dependency") === "1" ? 70 : 110);
      opts.edgeElasticity = 0.45;
      opts.gravity = 0.3;
      opts.gravityRange = 3.0;
      opts.nestingFactor = 0.1;
      opts.numIter = 2500;
      opts.randomize = true;
      opts.nodeSeparation = 90;
      opts.packComponents = true;
    } else if (real === "concentric") {
      // Workload(s) in the centre, then resources, then findings/overlays on the outer rings.
      const tier = (n: any): number => {
        const k = n.data("kind");
        if (k === "workload" || k === "tenant_connection") return 4;
        if (k === "architecture" || k === "architecture_memory") return 3;
        if (k === "resource" || k === "resource_group" || k === "subscription") return 2;
        return 1; // findings, coverage gaps, retirements, rbac, change, cost…
      };
      opts.concentric = tier;
      opts.levelWidth = () => 1;
      opts.minNodeSpacing = 22;
      opts.spacingFactor = 1.1;
    } else if (real === "cose") {
      opts.nodeRepulsion = 8000; opts.idealEdgeLength = 90; opts.randomize = false;
    }
    try {
      const l = cy.layout(opts);
      l.one("layoutstop", () => { try { cy.fit(undefined, 50); } catch { /* ignore */ } });
      l.run();
    } catch {
      cy.layout({ name: "cose", animate: false, fit: true, padding: 50 }).run();
    }
  }, []);

  const stampOwners = useCallback(async (nodes: GraphNode[]) => {
    // Resolve the effective owner of each workload node from the ownership registry and stamp
    // it into the node data so the Ownership lens colours by REAL assignments (not just tags).
    const subjects = nodes
      .filter((n) => n.kind === "workload" && n.data?.workload_id)
      .map((n) => ({ subject_kind: "workload", subject_id: String(n.data?.workload_id || "") }))
      .filter((s) => s.subject_id);
    if (subjects.length === 0) return;
    try {
      const res = await api.resolveOwnerBatch(subjects);
      const byId = new Map(res.results.map((r) => [r.subject_id, r]));
      for (const n of nodes) {
        if (n.kind !== "workload") continue;
        const r = byId.get(String(n.data?.workload_id || ""));
        if (!r || r.unowned || !r.owners.length) continue;
        const primary = r.owners.find((o) => o.primary) ?? r.owners[0];
        const label = primary.display_name || primary.email;
        if (!label) continue;
        const existing = nodeDataRef.current.get(n.id);
        if (existing) existing.data = { ...existing.data, owner: label, owner_source: r.source };
      }
      if (lensRef.current === "ownership") applyLens("ownership");
    } catch {
      /* ownership is best-effort decoration; never block the graph */
    }
  }, [applyLens]);

  const loadGraph = useCallback((result: GraphResult, layout = "breadthfirst") => {
    const cy = cyRef.current;
    if (!cy) return;
    expandedRef.current.clear(); // fresh canvas → forget prior expand/collapse tracking
    remember(result.nodes);
    cy.elements().remove();
    cy.add(toElements(result.nodes, result.edges, lens));
    applyHidden(hidden);
    applyLens(lens);
    void stampOwners(result.nodes);
    setStats({ nodes: result.nodes.length, edges: result.edges.length });
    // Defer layout + fit until the container actually has a non-zero size. A fresh remount
    // (navigate away → back to /graph) can report a 0×0 flex container for several frames; if
    // fcose + fit run then, every node is positioned/framed against a zero box and the canvas
    // paints BLANK even though the model has all its nodes (status still shows "N nodes"). Poll
    // a handful of frames for a real size, then lay out + fit. The normal (already-sized) case
    // runs immediately on the first attempt, so there's no regression on a fresh load.
    const layoutWhenSized = (attempt = 0) => {
      const c = cyRef.current;
      const el = containerRef.current;
      if (!c || !el) return;
      if ((el.clientWidth < 2 || el.clientHeight < 2) && attempt < 40) {
        requestAnimationFrame(() => layoutWhenSized(attempt + 1));
        return;
      }
      try { c.resize(); } catch { /* ignore */ }
      runLayout(layout);
    };
    layoutWhenSized();
  }, [remember, runLayout, applyHidden, applyLens, hidden, lens]);

  const mergeResult = useCallback((result: GraphResult, sourceId?: string): string[] => {
    const cy = cyRef.current;
    if (!cy) return [];
    remember(result.nodes);
    const newNodes = result.nodes.filter((n) => cy.getElementById(n.id).empty());
    const newEdges = result.edges.filter((e) => cy.getElementById(e.id).empty());
    const added = cy.add(toElements(newNodes, newEdges, lens));
    const src = sourceId ? cy.getElementById(sourceId) : null;
    if (src && src.nonempty() && newNodes.length) {
      const center = src.position();
      const radius = Math.max(90, Math.min(300, newNodes.length * 22));
      newNodes.forEach((n, i) => {
        const angle = (2 * Math.PI * i) / newNodes.length;
        cy.getElementById(n.id).position({ x: center.x + radius * Math.cos(angle), y: center.y + radius * Math.sin(angle) });
      });
      added.nodes().style("opacity", 0);
      added.nodes().animate({ style: { opacity: 1 } }, { duration: 300 });
    } else if (newNodes.length) {
      runLayout("organic");
    }
    applyHidden(hidden);
    applyLens(lens);
    setStats({ nodes: cy.nodes().length, edges: cy.edges().length });
    return newNodes.map((n) => n.id);
  }, [remember, runLayout, applyHidden, applyLens, hidden, lens]);

  const clearHighlights = useCallback(() => {
    cyRef.current?.elements().removeClass("path dim blast-direct blast-indirect highlight");
  }, []);

  const highlightNodes = useCallback((ids: string[], cls: string) => {
    const cy = cyRef.current;
    if (!cy) return;
    ids.forEach((id) => cy.getElementById(id).addClass(cls));
  }, []);

  // -------------------------------------------------- path / blast
  const runPath = useCallback(async (source: string, target: string) => {
    const { nodes, edges } = currentElements();
    try {
      const res = await api.graphPath(nodes, edges, source, target);
      clearHighlights();
      if (!res.found) setStatus("No path between those nodes on the current canvas.");
      else {
        const cy = cyRef.current!;
        cy.elements().addClass("dim");
        res.path.forEach((id) => cy.getElementById(id).removeClass("dim").addClass("path"));
        res.edges.forEach((id) => cy.getElementById(id).removeClass("dim").addClass("path"));
        setStatus(`Path found: ${res.hops} hop(s).`);
      }
    } catch (e) { setStatus(formatError(e)); }
    finally { setPathSource(null); setMode("explore"); }
  }, [currentElements, clearHighlights]);

  const runBlast = useCallback(async (source: string) => {
    const { nodes, edges } = currentElements();
    try {
      const res = await api.graphBlastRadius(nodes, edges, source, 3);
      clearHighlights();
      const cy = cyRef.current!;
      cy.elements().addClass("dim");
      cy.getElementById(source).removeClass("dim").addClass("highlight");
      res.direct.forEach((id) => cy.getElementById(id).removeClass("dim").addClass("blast-direct"));
      res.indirect.forEach((id) => cy.getElementById(id).removeClass("dim").addClass("blast-indirect"));
      setStatus(`Blast radius: ${res.impacted_count} node(s) impacted, ${res.impacted_workloads.length} workload(s).`);
    } catch (e) { setStatus(formatError(e)); }
    finally { setMode("explore"); }
  }, [currentElements, clearHighlights]);

  // -------------------------------------------------- findings clustering (item 6)
  // Collapse 8+ findings on one workload into a single "N findings" super-node. The hidden
  // members are stashed so a tap re-expands them. Keeps the dense focus view readable.
  const collapseFindings = useCallback((result: GraphResult): GraphResult => {
    collapsedRef.current.clear();
    const findingNodes = new Map(result.nodes.filter((n) => n.kind === "assessment_finding").map((n) => [n.id, n]));
    if (findingNodes.size < 8) return result;
    const bySource = new Map<string, string[]>();
    for (const e of result.edges) {
      if (e.kind === "has_finding" && findingNodes.has(e.target)) {
        const arr = bySource.get(e.source) || [];
        arr.push(e.target);
        bySource.set(e.source, arr);
      }
    }
    const remove = new Set<string>();
    const superNodes: GraphNode[] = [];
    const superEdges: GraphEdge[] = [];
    bySource.forEach((ids, src) => {
      if (ids.length < 8) return;
      const superId = `findings:${src}`;
      const groupNodes = ids.map((i) => findingNodes.get(i)!).filter(Boolean);
      const groupEdges = result.edges.filter((e) => e.kind === "has_finding" && e.source === src && ids.includes(e.target));
      collapsedRef.current.set(superId, { nodes: groupNodes, edges: groupEdges });
      ids.forEach((i) => remove.add(i));
      superNodes.push({ id: superId, kind: "assessment_finding", label: `⚠ ${ids.length} findings`, data: { collapsed: true, count: ids.length, source: src }, badges: {}, expandable: true } as GraphNode);
      superEdges.push({ id: `${src}__has_finding__${superId}`, source: src, target: superId, kind: "has_finding", label: "" });
    });
    if (!superNodes.length) return result;
    const nodes = result.nodes.filter((n) => !remove.has(n.id)).concat(superNodes);
    const edges = result.edges.filter((e) => !(e.kind === "has_finding" && remove.has(e.target))).concat(superEdges);
    return { ...result, nodes, edges };
  }, []);

  const expandCollapsed = useCallback((superId: string) => {
    const cy = cyRef.current;
    if (!cy) return;
    const grp = collapsedRef.current.get(superId);
    if (!grp) return;
    collapsedRef.current.delete(superId);
    const sup = cy.getElementById(superId);
    const center = sup.nonempty() ? sup.position() : { x: 0, y: 0 };
    cy.remove(sup);
    remember(grp.nodes);
    const added = cy.add(toElements(grp.nodes, grp.edges, lens));
    grp.nodes.forEach((n, i) => {
      const a = (2 * Math.PI * i) / grp.nodes.length;
      cy.getElementById(n.id).position({ x: center.x + 80 * Math.cos(a), y: center.y + 80 * Math.sin(a) });
    });
    added.style("opacity", 0);
    added.animate({ style: { opacity: 1 } }, { duration: 250 });
    applyHidden(hidden);
    applyLens(lens);
    setStats({ nodes: cy.nodes().length, edges: cy.edges().length });
    snapshotRef.current("push");
  }, [remember, applyHidden, applyLens, hidden, lens]);

  // Collapse a previously-expanded node: remove the children it added (recursively for any of
  // those children that were themselves expanded), but keep any child still referenced by
  // ANOTHER expanded parent that isn't being collapsed (shared resources stay put).
  const collapseNode = useCallback((nodeId: string): number => {
    const cy = cyRef.current;
    if (!cy) return 0;
    const collapsing = new Set<string>([nodeId]);
    const candidates = new Set<string>();
    const stack = [nodeId];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const child of expandedRef.current.get(cur) || []) {
        candidates.add(child);
        if (expandedRef.current.has(child)) { collapsing.add(child); stack.push(child); }
      }
    }
    const keep = new Set<string>();
    expandedRef.current.forEach((kids, parent) => {
      if (collapsing.has(parent)) return;
      kids.forEach((k) => { if (candidates.has(k)) keep.add(k); });
    });
    const remove = [...candidates].filter((id) => !keep.has(id));
    cy.batch(() => { remove.forEach((id) => cy.getElementById(id).remove()); });
    collapsing.forEach((id) => expandedRef.current.delete(id));
    applyHidden(hidden);
    applyLens(lens);
    setStats({ nodes: cy.nodes().length, edges: cy.edges().length });
    if (remove.length) snapshotRef.current("push");
    return remove.length;
  }, [applyHidden, applyLens, hidden, lens]);

  const expandNode = useCallback(async (nodeId: string) => {
    // Findings super-node: route to its own expander (single + double tap behave the same).
    if (nodeId.startsWith("findings:")) { expandCollapsed(nodeId); return; }
    // Toggle: a second double-click on an already-expanded node collapses it.
    if (expandedRef.current.has(nodeId)) {
      const n = collapseNode(nodeId);
      setStatus(n ? `Collapsed ${n} node(s).` : "Collapsed.");
      setCtx(null);
      return;
    }
    setExpanding(true);
    try {
      const result = await api.graphExpand(nodeId, effectiveConn);
      const addedIds = mergeResult(result, nodeId);
      if (addedIds.length) { expandedRef.current.set(nodeId, addedIds); snapshotRef.current("push"); }
      if (result.truncated) setStatus("Showing a capped set of children (large group).");
    } catch (e) { setStatus(formatError(e)); }
    finally { setExpanding(false); setCtx(null); }
  }, [effectiveConn, mergeResult, collapseNode, expandCollapsed]);

  // -------------------------------------------------- node tap (mode-aware)
  const onNodeTap = useCallback((nodeId: string) => {
    setCtx(null);
    if (nodeId.startsWith("findings:")) { expandCollapsed(nodeId); return; }
    if (mode === "path") {
      if (!pathSource) { setPathSource(nodeId); setStatus("Path: pick a target node"); highlightNodes([nodeId], "path"); }
      else void runPath(pathSource, nodeId);
      return;
    }
    if (mode === "blast") { void runBlast(nodeId); return; }
    setSelected(nodeId);
  }, [mode, pathSource, runPath, runBlast, highlightNodes, expandCollapsed]);

  // -------------------------------------------------- focus scope (overlays + drift)
  const focus = useCallback(async (kind: string, id: string) => {
    setExpanding(true);
    setSelected(null);
    try {
      const result = await api.graphBuild(kind, id, { connectionId: effectiveConn, overlays: [...overlays], drift: driftMode });
      setFocusScope({ kind, id });
      loadGraph(collapseFindings(result), "concentric");
      snapshotRef.current("push");
      const driftMsg = (result as any).drift?.summary ? ` — ${(result as any).drift.summary}` : "";
      setStatus(`Focused ${kind} · ${result.nodes.length} nodes${driftMsg}`);
    } catch (e) { setStatus(formatError(e)); }
    finally { setExpanding(false); setCtx(null); }
  }, [effectiveConn, overlays, driftMode, loadGraph, collapseFindings]);

  // Focus on one OR MORE workloads at once (merged subgraph).
  const focusWorkloads = useCallback(async (ids: string[]) => {
    if (ids.length === 0) return;
    setExpanding(true);
    setSelected(null);
    try {
      const result = await api.graphBuildWorkloads(ids, { connectionId: effectiveConn, overlays: [...overlays], drift: driftMode });
      setFocusScope({ kind: "workloads", ids });
      loadGraph(collapseFindings(result), "concentric");
      snapshotRef.current("push");
      setStatus(`Focused ${result.workload_count ?? ids.length} workload(s) · ${result.nodes.length} nodes`);
    } catch (e) { setStatus(formatError(e)); }
    finally { setExpanding(false); setCtx(null); }
  }, [effectiveConn, overlays, driftMode, loadGraph, collapseFindings]);

  const backToOverview = useCallback(() => {
    setFocusScope(null);
    setDriftMode(false);
    if (overviewQ.data) { loadGraph(overviewQ.data, layoutRef.current); snapshotRef.current("push"); }
  }, [overviewQ.data, loadGraph]);

  // Full reset: clear highlights/filters/selection/modes/overlays and reload the clean overview
  // (which also forgets all expand/collapse state), then fit.
  const resetView = useCallback(() => {
    clearHighlights();
    setMode("explore");
    setPathSource(null);
    setSelected(null);
    setCtx(null);
    setHidden(new Set());
    setOverlays(new Set());
    setDriftMode(false);
    setFocusScope(null);
    setSelectedWls(new Set());
    setWlFilter("");
    if (overviewQ.data) loadGraph(overviewQ.data, layoutRef.current);
    setStatus("View reset.");
    snapshotRef.current("reset");
  }, [clearHighlights, overviewQ.data, loadGraph]);

  // -------------------------------------------------- undo / redo history
  const captureSnapshot = useCallback((): GraphSnapshot | null => {
    const cy = cyRef.current;
    if (!cy) return null;
    return {
      elements: cy.elements().jsons() as any[],
      pan: { ...cy.pan() },
      zoom: cy.zoom(),
      expanded: [...expandedRef.current.entries()].map(([k, v]) => [k, [...v]] as [string, string[]]),
      collapsed: [...collapsedRef.current.entries()].map(([k, v]) => [k, { nodes: [...v.nodes], edges: [...v.edges] }] as [string, { nodes: GraphNode[]; edges: GraphEdge[] }]),
      focusScope: focusScopeRef.current,
      stats: { nodes: cy.nodes().length, edges: cy.edges().length },
    };
  }, []);

  // Snapshot the canvas once the layout has settled. mode "reset" starts a fresh history at the
  // current state (used for the overview baseline + Reset view); mode "push" appends a new step
  // and drops any "redo" branch ahead of the current position. Deferred so force-directed layouts
  // have applied their final node positions before we capture them.
  const snapshotAfterSettle = useCallback((mode: "reset" | "push") => {
    window.setTimeout(() => {
      const snap = captureSnapshot();
      if (!snap) return;
      if (mode === "reset") {
        historyRef.current = [snap];
        historyPosRef.current = 0;
      } else {
        const hist = historyRef.current;
        hist.splice(historyPosRef.current + 1);
        hist.push(snap);
        const CAP = 60;
        if (hist.length > CAP) hist.splice(0, hist.length - CAP);
        historyPosRef.current = hist.length - 1;
      }
      setHistState({ canUndo: historyPosRef.current > 0, canRedo: historyPosRef.current < historyRef.current.length - 1 });
    }, 360);
  }, [captureSnapshot]);

  // Rebuild the canvas from a snapshot (used by undo/redo). Pure teardown/rebuild — it does not
  // itself record history, so the history stack is only ever changed by snapshotAfterSettle.
  const restoreSnapshot = useCallback((snap: GraphSnapshot) => {
    const cy = cyRef.current;
    if (!cy) return;
    expandedRef.current = new Map(snap.expanded.map(([k, v]) => [k, [...v]]));
    collapsedRef.current = new Map(snap.collapsed.map(([k, v]) => [k, { nodes: v.nodes, edges: v.edges }]));
    cy.elements().remove();
    cy.add(snap.elements.filter((e: any) => e.group === "nodes"));
    cy.add(snap.elements.filter((e: any) => e.group === "edges"));
    applyHidden(hidden);
    applyLens(lens);
    cy.zoom(snap.zoom);
    cy.pan({ ...snap.pan });
    setStats(snap.stats);
    setFocusScope(snap.focusScope);
    setSelected(null);
    setCtx(null);
  }, [applyHidden, applyLens, hidden, lens]);

  const undo = useCallback(() => {
    if (historyPosRef.current <= 0) return;
    historyPosRef.current -= 1;
    restoreSnapshot(historyRef.current[historyPosRef.current]);
    setHistState({ canUndo: historyPosRef.current > 0, canRedo: historyPosRef.current < historyRef.current.length - 1 });
    setStatus("Undid last change.");
  }, [restoreSnapshot]);

  const redo = useCallback(() => {
    if (historyPosRef.current >= historyRef.current.length - 1) return;
    historyPosRef.current += 1;
    restoreSnapshot(historyRef.current[historyPosRef.current]);
    setHistState({ canUndo: historyPosRef.current > 0, canRedo: historyPosRef.current < historyRef.current.length - 1 });
    setStatus("Redid change.");
  }, [restoreSnapshot]);

  // Stable refs so the one-time keyboard handler always calls the latest undo/redo closures.
  const snapshotRef = useRef(snapshotAfterSettle);
  const undoRef = useRef(undo);
  const redoRef = useRef(redo);
  useEffect(() => { snapshotRef.current = snapshotAfterSettle; undoRef.current = undo; redoRef.current = redo; }, [snapshotAfterSettle, undo, redo]);

  // Switch the overview layout ("view"), remember it server-side for this tenant, and re-run.
  const applyLayout = useCallback((l: GLayout) => {
    setLayout(l);
    layoutRef.current = l;
    runLayout(l);
    api.graphSavePrefs(effectiveTenant, l).catch(() => { /* best-effort */ });
  }, [runLayout, effectiveTenant]);

  // Apply the server-remembered layout when it arrives / the tenant changes. Re-lays out the
  // overview in place (focus views keep their concentric layout).
  useEffect(() => {
    const remembered = (prefsQ.data?.layout as GLayout) || "organic";
    setLayout(remembered);
    layoutRef.current = remembered;
    const cy = cyRef.current;
    if (cy && cy.elements().length > 0 && !focusScope) runLayout(remembered);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefsQ.data, effectiveTenant]);

  // -------------------------------------------------- cytoscape lifecycle
  useEffect(() => {
    if (!containerRef.current || cyRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      style: buildStylesheet(lens, dark),
      minZoom: 0.05,
      maxZoom: 4,
      wheelSensitivity: 0.25,
      boxSelectionEnabled: true,
    });
    cyRef.current = cy;
    setCyReady(true);

    cy.on("tap", "node", (evt: EventObject) => onNodeTapRef.current(evt.target.id()));
    cy.on("dbltap", "node", (evt: EventObject) => void expandNodeRef.current(evt.target.id()));
    cy.on("cxttap", "node", (evt: EventObject) => {
      const pos = evt.renderedPosition || { x: 0, y: 0 };
      setCtx({ x: pos.x, y: pos.y, nodeId: evt.target.id(), kind: evt.target.data("kind") });
      setQuickCard(null);
    });
    cy.on("mouseover", "node", (evt: EventObject) => {
      const pos = evt.renderedPosition || { x: 0, y: 0 };
      evt.target.addClass("hover");
      const full = nodeDataRef.current.get(evt.target.id());
      if (full) setQuickCard({ x: pos.x, y: pos.y, node: full });
    });
    cy.on("mouseout", "node", (evt: EventObject) => { evt.target.removeClass("hover"); setQuickCard(null); });
    cy.on("cxttap", (evt: EventObject) => {
      if (evt.target === cy) { const pos = evt.renderedPosition || { x: 0, y: 0 }; setCtx({ x: pos.x, y: pos.y }); }
    });
    cy.on("tap", (evt: EventObject) => {
      if (evt.target === cy) { setCtx(null); setSelected(null); setQuickCard(null); }
    });
    cy.on("zoom", () => {
      const z = cy.zoom();
      cy.batch(() => {
        cy.nodes().forEach((n) => {
          const big = ["tenant_connection", "management_group", "subscription", "workload"].includes(n.data("kind"));
          n.style("text-opacity", z < 0.35 && !big ? 0 : 1);
        });
      });
    });
    // Any viewport change (pan or zoom) invalidates the screen coordinates the context menu /
    // hover card were anchored at, so dismiss them — otherwise they float at a stale spot while the
    // graph slides underneath (and still act on the original node).
    cy.on("pan zoom", () => { setCtx(null); setQuickCard(null); });

    // Keep cytoscape's backing store in sync with the container size (rail toggle, window resize,
    // panel open/close). A desynced canvas mis-maps click / right-click hit-testing.
    let resizeRaf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(() => { try { cy.resize(); } catch { /* ignore */ } });
    });
    ro.observe(containerRef.current);

    return () => { ro.disconnect(); cancelAnimationFrame(resizeRaf); cy.destroy(); cyRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Stable refs so the one-time cytoscape handlers always call the latest closures.
  const onNodeTapRef = useRef(onNodeTap);
  const expandNodeRef = useRef(expandNode);
  useEffect(() => { onNodeTapRef.current = onNodeTap; expandNodeRef.current = expandNode; }, [onNodeTap, expandNode]);

  useEffect(() => { cyRef.current?.style(buildStylesheet(lens, dark)); applyLens(lens); }, [lens, dark, applyLens]);

  // Marching-ants animation on data-flow / dependency edges (item 10). Lightweight rAF that
  // decrements line-dash-offset on the .flow edges; pauses when there are none.
  useEffect(() => {
    let raf = 0;
    let off = 0;
    const tick = () => {
      const cy = cyRef.current;
      if (cy) {
        const flows = cy.edges(".flow:visible");
        if (flows.nonempty()) {
          off = (off - 0.6) % 1000;
          flows.style("line-dash-offset", off);
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  // Auto-render the overview when its data first arrives (or genuinely changes). Guarded by a ref
  // so that merely returning to overview (backToOverview / an undo that clears focusScope) does NOT
  // re-run this effect — those paths render + record history themselves, and a reload here would
  // clobber a restored snapshot and reset the undo stack.
  const loadedOverviewRef = useRef<unknown>(null);
  useEffect(() => {
    if (!overviewQ.data || focusScope) return;
    // The live cytoscape instance is recreated on every (re)mount — navigating away to another
    // SPA route and back destroys the old `cy` and builds a fresh, EMPTY one. The
    // `loadedOverviewRef` guard (which prevents a redundant reload on a focusScope toggle) would
    // otherwise early-return on that remount because `overviewQ.data` is still the same cached
    // object — leaving the new canvas blank even though the status line says "N nodes". So also
    // reload whenever the current instance has no elements. `cyReady` is in the deps so this runs
    // once the fresh instance exists.
    const cy = cyRef.current;
    const cyEmpty = !cy || cy.elements().length === 0;
    if (loadedOverviewRef.current === overviewQ.data && !cyEmpty) return;
    if (!cy) return; // wait for the cytoscape instance (cyReady will re-trigger us)
    loadedOverviewRef.current = overviewQ.data;
    loadGraph(overviewQ.data, layoutRef.current);
    snapshotRef.current("reset");
    const d = overviewQ.data;
    setStatus(d.inventory_loaded
      ? `${d.counts.workloads} workloads · ${d.counts.subscriptions} subs · ${d.counts.architectures} architectures · inventory cached`
      : `${d.counts.workloads} workloads · ${d.counts.architectures} architectures · inventory not scanned`);
    if (focusId) {
      const cy = cyRef.current;
      const t = cy?.getElementById(focusId);
      if (t && t.nonempty()) { setSelected(focusId); cy!.animate({ center: { eles: t }, zoom: 1.2 }, { duration: 350 }); }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overviewQ.data, focusScope, cyReady]);

  // Re-focus when overlays/drift change while focused.
  useEffect(() => {
    if (!focusScope) return;
    if (focusScope.kind === "workloads" && focusScope.ids) void focusWorkloads(focusScope.ids);
    else if (focusScope.id) void focus(focusScope.kind, focusScope.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overlays, driftMode]);

  // -------------------------------------------------- search
  const runSearch = useCallback(async (term: string) => {
    setSearchTerm(term);
    if (!term.trim()) { setSearchResults([]); return; }
    try { setSearchResults((await api.graphSearch(term, effectiveConn)).nodes); }
    catch { setSearchResults([]); }
  }, [effectiveConn]);

  const focusNode = useCallback((node: GraphNode) => {
    const cy = cyRef.current;
    if (!cy) return;
    remember([node]);
    let target = cy.getElementById(node.id);
    if (target.empty()) { cy.add(toElements([node], [], lens)); target = cy.getElementById(node.id); runLayout("organic"); }
    setSelected(node.id);
    cy.animate({ center: { eles: target }, zoom: 1.3 }, { duration: 350 });
    setSearchResults([]); setSearchTerm("");
  }, [remember, runLayout, lens]);

  const onAskMatched = useCallback((matched: GraphNode[]) => {
    const cy = cyRef.current;
    if (!cy) return;
    remember(matched);
    const missing = matched.filter((n) => cy.getElementById(n.id).empty());
    if (missing.length) cy.add(toElements(missing, [], lens));
    clearHighlights();
    cy.elements().addClass("dim");
    matched.forEach((n) => cy.getElementById(n.id).removeClass("dim").addClass("highlight"));
    if (missing.length) runLayout("cose");
    setStatus(`Highlighted ${matched.length} matched node(s).`);
    snapshotRef.current("push");
  }, [remember, clearHighlights, runLayout, lens]);

  // -------------------------------------------------- toggles
  const toggleKind = (k: GraphNodeKind) => {
    setHidden((prev) => { const next = new Set(prev); if (next.has(k)) next.delete(k); else next.add(k); applyHidden(next); return next; });
  };
  const toggleOverlay = (o: string) => setOverlays((prev) => { const next = new Set(prev); if (next.has(o)) next.delete(o); else next.add(o); return next; });

  // -------------------------------------------------- saved views
  const saveCurrentView = useCallback(async (name: string) => {
    const cy = cyRef.current;
    const scopeId = focusScope?.kind === "workloads" ? (focusScope.ids || []).join(",") : (focusScope?.id || "");
    await api.graphSaveView({
      name, connection_id: effectiveConn, scope_kind: focusScope?.kind || "overview", scope_id: scopeId,
      lens, layout: "cose", hidden_kinds: [...hidden], overlays: [...overlays],
      camera: cy ? { zoom: cy.zoom(), pan: cy.pan() } : {},
    });
  }, [effectiveConn, focusScope, lens, hidden, overlays]);

  const applyView = useCallback((v: GraphView) => {
    setLens((v.lens as Lens) || "none");
    setHidden(new Set(v.hidden_kinds as GraphNodeKind[]));
    setOverlays(new Set(v.overlays || []));
    if (v.scope_kind === "overview") backToOverview();
    else if (v.scope_kind === "workloads") {
      const ids = (v.scope_id || "").split(",").filter(Boolean);
      setSelectedWls(new Set(ids));
      void focusWorkloads(ids);
    } else void focus(v.scope_kind, v.scope_id);
    const cy = cyRef.current;
    if (cy && v.camera?.zoom) setTimeout(() => { cy.zoom(v.camera.zoom); if (v.camera.pan) cy.pan(v.camera.pan); }, 500);
    setLeftPanel("none");
  }, [setLens, backToOverview, focus, focusWorkloads]);

  // -------------------------------------------------- guided tour
  const TOUR = useMemo(() => {
    const cy = cyRef.current;
    const has = (kind: string) => cy?.nodes(`[kind = "${kind}"]`).nonempty();
    return [
      { text: "This is your estate graph — the tenant connection at the root.", kind: "tenant_connection" },
      { text: "Subscriptions hang off the connection.", kind: "subscription" },
      { text: "Workloads are the primary objects — colour them by the Risk lens.", kind: "workload" },
      { text: "Architectures model workloads; memory documents them.", kind: "architecture" },
    ].filter((s) => has(s.kind));
  }, [stats.nodes]);

  useEffect(() => {
    const cy = cyRef.current;
    if (tourStep < 0 || !cy) return;
    const step = TOUR[tourStep];
    if (!step) { setTourStep(-1); return; }
    const node = cy.nodes(`[kind = "${step.kind}"]`).first();
    if (node.nonempty()) { setSelected(node.id()); cy.animate({ center: { eles: node }, zoom: 1.4 }, { duration: 500 }); }
  }, [tourStep, TOUR]);

  // keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) { e.preventDefault(); if (e.shiftKey) redoRef.current(); else undoRef.current(); return; }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); redoRef.current(); return; }
      if (e.key === "f") cyRef.current?.fit(undefined, 40);
      else if (e.key === "Escape") { clearHighlights(); setMode("explore"); setPathSource(null); setSelected(null); setCtx(null); }
      else if (e.key === "b" && selected) void runBlast(selected);
      else if (e.key === "/") { e.preventDefault(); (document.querySelector("input[placeholder^='Search']") as HTMLInputElement)?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, clearHighlights, runBlast]);

  const selKind: GraphNodeKind | "" = (detailQ.data?.node?.kind as GraphNodeKind) || (selected?.startsWith("wl:") ? "workload" : "");

  // -------------------------------------------------- workload filter (multi-select)
  const scopedWorkloads = useMemo(() => {
    const nodes = overviewQ.data?.nodes || [];
    return nodes
      .filter((n) => n.kind === "workload")
      .map((n) => ({ id: n.id.slice(3), label: n.label, risk: (n.data?.risk?.level as string) || "" }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [overviewQ.data]);

  const filteredWorkloads = useMemo(() => {
    const q = wlFilter.trim().toLowerCase();
    return q ? scopedWorkloads.filter((w) => w.label.toLowerCase().includes(q)) : scopedWorkloads;
  }, [scopedWorkloads, wlFilter]);

  const toggleWl = (id: string) =>
    setSelectedWls((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const clearWls = () => { setSelectedWls(new Set()); };
  // Selecting workloads while focused on them keeps the canvas in sync on re-focus.
  const RISK_DOT: Record<string, string> = { ok: "bg-emerald-500", low: "bg-lime-500", medium: "bg-amber-500", high: "bg-red-500" };

  // -------------------------------------------------- open in Azure Portal
  const openPortal = useCallback((nodeId: string) => {
    const node = nodeDataRef.current.get(nodeId);
    const url = node ? azurePortalUrl(node, effectiveTenant) : "";
    if (url) window.open(url, "_blank", "noopener,noreferrer");
    setCtx(null);
  }, [effectiveTenant]);


  // -------------------------------------------------- render
  return (
    <div className={`flex h-full min-h-0 flex-col ${dark ? "bg-slate-900" : "bg-slate-50"}`}>
      {/* Command bar (single row; secondary view actions tucked under a ⋯ View menu) */}
      <div className={`flex flex-wrap items-center gap-2 border-b px-4 py-2 ${dark ? "border-slate-700 bg-slate-800" : "bg-white"}`}>
        <div className="flex items-center gap-2"><span className="text-base">🕸️</span><span className={`font-semibold ${dark ? "text-slate-100" : "text-slate-800"}`}>Estate Graph</span></div>
        <div className="relative">
          <input
            value={searchTerm}
            onChange={(e) => void runSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") { setSearchResults([]); setSearchTerm(""); (e.target as HTMLInputElement).blur(); } }}
            onBlur={() => window.setTimeout(() => setSearchResults([]), 150)}
            placeholder="Search… ( / )"
            className={`w-60 rounded-md border px-3 py-1.5 text-sm focus:border-brand focus:outline-none ${dark ? "border-slate-600 bg-slate-700 text-slate-100 placeholder:text-slate-400" : "border-slate-300"}`}
          />
          {searchResults.length > 0 && (
            <div className="absolute z-30 mt-1 max-h-80 w-72 overflow-auto rounded-md border bg-white shadow-lg">
              {searchResults.map((n) => (
                <button key={n.id} onClick={() => focusNode(n)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-slate-50">
                  <span>{KIND_META[n.kind]?.glyph}</span><span className="truncate">{n.label}</span>
                  <span className="ml-auto text-[10px] uppercase text-slate-400">{KIND_META[n.kind]?.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <select value={lens} onChange={(e) => setLens(e.target.value as Lens)} className={`rounded-md border px-2 py-1.5 text-sm ${dark ? "border-slate-600 bg-slate-700 text-slate-100" : "border-slate-300"}`} title="Lens">
          {LENSES.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
        <select value={effectiveConn} onChange={(e) => setConnectionId(e.target.value)} className={`rounded-md border px-2 py-1.5 text-sm ${dark ? "border-slate-600 bg-slate-700 text-slate-100" : "border-slate-300"}`} title="Azure connection">
          {(connQ.data?.connections || []).map((c) => <option key={c.id} value={c.id}>{c.display_name || c.tenant_id || c.id}{c.is_default ? " (default)" : ""}</option>)}
        </select>
        <div className="ml-auto flex items-center gap-1.5">
          <IconBtn dark={dark} title="Undo (Ctrl+Z)" disabled={!histState.canUndo} onClick={undo}>↶</IconBtn>
          <IconBtn dark={dark} title="Redo (Ctrl+Shift+Z)" disabled={!histState.canRedo} onClick={redo}>↷</IconBtn>
          <span className={`mx-0.5 h-5 w-px ${dark ? "bg-slate-600" : "bg-slate-300"}`} />
          <ToolBtn dark={dark} active={leftPanel === "ask"} onClick={() => setLeftPanel(leftPanel === "ask" ? "none" : "ask")}>Ask</ToolBtn>
          <ToolBtn dark={dark} active={leftPanel === "analytics"} onClick={() => setLeftPanel(leftPanel === "analytics" ? "none" : "analytics")}>Analytics</ToolBtn>
          <ToolBtn dark={dark} active={leftPanel === "views"} onClick={() => setLeftPanel(leftPanel === "views" ? "none" : "views")}>Views</ToolBtn>
          <ToolBtn dark={dark} onClick={() => { clearHighlights(); cyRef.current?.fit(undefined, 40); }}>Fit</ToolBtn>
          <div className="relative">
            <ToolBtn dark={dark} active={viewMenu} onClick={() => setViewMenu((v) => !v)}>⋯ View</ToolBtn>
            {viewMenu && (
              <div className="absolute right-0 z-40 mt-1 w-48 overflow-hidden rounded-md border bg-white py-1 text-sm shadow-xl" onMouseLeave={() => setViewMenu(false)}>
                <div className="px-3 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Layout (remembered per tenant)</div>
                <MI label={`${layout === "organic" ? "✓ " : ""}Organic layout`} onClick={() => { applyLayout("organic"); setViewMenu(false); }} />
                <MI label={`${layout === "hierarchy" ? "✓ " : ""}Hierarchy layout`} onClick={() => { applyLayout("hierarchy"); setViewMenu(false); }} />
                <MI label={`${layout === "concentric" ? "✓ " : ""}Radial layout`} onClick={() => { applyLayout("concentric"); setViewMenu(false); }} />
                <div className="my-1 border-t" />
                <MI label="Guided tour" onClick={() => { setTourStep(0); setViewMenu(false); }} />
                <MI label={dark ? "Light canvas" : "Dark canvas"} onClick={() => { setDark(!dark); setViewMenu(false); }} />
                <MI label={railOpen ? "Hide left rail" : "Show left rail"} onClick={() => { setRailOpen(!railOpen); setViewMenu(false); }} />
                <div className="my-1 border-t" />
                <MI label="↺ Reset view" onClick={() => { resetView(); setViewMenu(false); }} />
                <MI label="Export PNG" onClick={() => { exportPng(cyRef.current, dark); setViewMenu(false); }} />
                <MI label="Export JSON" onClick={() => { exportJson(currentElements()); setViewMenu(false); }} />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Mode / overlay strip (compact) */}
      <div className={`flex flex-wrap items-center gap-1.5 border-b px-4 py-1.5 text-xs ${dark ? "border-slate-700 bg-slate-800/60" : "bg-slate-50"}`}>
        {focusScope && <button onClick={backToOverview} className="rounded-md bg-slate-800 px-2 py-1 font-medium text-white">← Overview</button>}
        {(["explore", "path", "blast"] as Mode[]).map((m) => (
          <button key={m} onClick={() => { setMode(m); setPathSource(null); clearHighlights(); }} className={`rounded-md px-2 py-1 capitalize ${mode === m ? "bg-brand text-white" : dark ? "border border-slate-600 text-slate-300 hover:bg-slate-700" : "border border-slate-300 hover:bg-slate-50"}`} title={`${m} mode`}>{m}</button>
        ))}
        <span className={`ml-2 ${dark ? "text-slate-500" : "text-slate-400"}`}>Overlays</span>
        {OVERLAY_OPTS.map((o) => (
          <button key={o.id} onClick={() => toggleOverlay(o.id)} className={`rounded-md px-2 py-1 ${overlays.has(o.id) ? "bg-emerald-600 text-white" : dark ? "border border-slate-600 text-slate-300 hover:bg-slate-700" : "border border-slate-300 hover:bg-slate-50"}`}>{o.label}</button>
        ))}
        <button onClick={() => setDriftMode((v) => !v)} className={`rounded-md px-2 py-1 ${driftMode ? "bg-violet-600 text-white" : dark ? "border border-slate-600 text-slate-300 hover:bg-slate-700" : "border border-slate-300 hover:bg-slate-50"}`} title="Intent vs reality">Drift</button>
        {!focusScope && <span className={`text-[11px] ${dark ? "text-slate-500" : "text-slate-400"}`}>· overlays + drift apply when you Focus a workload</span>}
      </div>

      <div className="relative flex min-h-0 flex-1">
        {/* Left rail (collapsible) */}
        {railOpen && (
        <div className={`hidden w-52 shrink-0 flex-col gap-3 overflow-y-auto border-r px-3 py-3 lg:flex ${dark ? "border-slate-700 bg-slate-800 text-slate-200" : "bg-white"}`}>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Layers</span>
            <button onClick={() => setRailOpen(false)} className="text-slate-400 hover:text-slate-600" title="Collapse rail">«</button>
          </div>
          <div>
            <div className="flex flex-col gap-0.5">
              {ALL_KINDS.map((k) => (
                <label key={k} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-slate-50/10">
                  <input type="checkbox" checked={!hidden.has(k)} onChange={() => toggleKind(k)} />
                  <span
                    className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded bg-white bg-contain bg-center bg-no-repeat"
                    style={{ border: `1.5px solid ${KIND_META[k].color}`, backgroundImage: `url("${kindIconUri(k)}")`, backgroundSize: "70%" }}
                  />
                  <span className="truncate text-slate-700">{KIND_META[k].label}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Workloads filter — pick one or more, then Focus */}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Workloads</span>
              {selectedWls.size > 0 && (
                <button onClick={clearWls} className="text-[10px] text-slate-400 hover:text-slate-600">Clear</button>
              )}
            </div>
            <input
              value={wlFilter}
              onChange={(e) => setWlFilter(e.target.value)}
              placeholder="Filter workloads…"
              className="mb-1 w-full rounded-md border border-slate-300 px-2 py-1 text-xs focus:border-brand focus:outline-none"
            />
            <div className="flex items-center justify-between px-0.5 pb-1 text-[10px] text-slate-400">
              <button
                onClick={() => setSelectedWls(new Set(filteredWorkloads.map((w) => w.id)))}
                className="hover:text-slate-600"
              >
                Select all{wlFilter ? " (filtered)" : ""}
              </button>
              <span>{selectedWls.size} selected</span>
            </div>
            <div className="max-h-52 overflow-y-auto">
              {filteredWorkloads.length === 0 && <div className="px-1 py-1 text-[11px] text-slate-400">No workloads.</div>}
              {filteredWorkloads.map((w) => (
                <label key={w.id} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-slate-50">
                  <input type="checkbox" checked={selectedWls.has(w.id)} onChange={() => toggleWl(w.id)} />
                  <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${RISK_DOT[w.risk] || "bg-slate-300"}`} />
                  <span className="truncate text-slate-700">{w.label}</span>
                </label>
              ))}
            </div>
            <div className="mt-1.5 flex gap-1.5">
              <button
                onClick={() => void focusWorkloads([...selectedWls])}
                disabled={selectedWls.size === 0}
                className="flex-1 rounded-md bg-brand px-2 py-1.5 text-xs font-medium text-white disabled:opacity-40"
              >
                Focus {selectedWls.size || ""}
              </button>
            </div>
          </div>

          {driftMode && (
            <div>
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Drift</div>
              <Legend color="#059669" label="Documented + live" />
              <Legend color="#d97706" label="Documented, missing" />
              <Legend color="#dc2626" label="Live, undocumented" />
            </div>
          )}
          <div className="mt-auto text-[11px] leading-relaxed text-slate-400">Tap to inspect · double-tap to expand · right-click for actions · keys: f=fit, b=blast, /=search, esc=clear.</div>
        </div>
        )}
        {/* Collapsed-rail re-open tab */}
        {!railOpen && (
          <button
            onClick={() => setRailOpen(true)}
            className={`absolute left-0 top-1/2 z-20 hidden -translate-y-1/2 rounded-r-md border border-l-0 px-1.5 py-2 text-xs shadow-sm lg:block ${dark ? "border-slate-700 bg-slate-800 text-slate-300" : "border-slate-200 bg-white text-slate-500"}`}
            title="Show left rail"
          >»</button>
        )}

        {/* Canvas */}
        <div className="relative min-h-0 flex-1">
          <div ref={containerRef} className={`graph-canvas absolute inset-0 ${dark ? "graph-canvas-dark" : ""}`} />
          {overviewQ.isLoading && <div className="absolute inset-0 flex items-center justify-center text-sm text-slate-500">Loading estate graph…</div>}
          {overviewQ.isError && <div className="absolute inset-0 flex items-center justify-center text-sm text-red-600">{formatError(overviewQ.error)}</div>}
          {/* Empty / sparse state (item 27) */}
          {!overviewQ.isLoading && !focusScope && stats.nodes <= 1 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
              <div className={`pointer-events-auto max-w-xs rounded-xl border p-5 text-center shadow-sm ${dark ? "border-slate-700 bg-slate-800 text-slate-300" : "border-slate-200 bg-white"}`}>
                <div className="text-2xl">🕸️</div>
                <div className={`mt-1 font-semibold ${dark ? "text-slate-100" : "text-slate-800"}`}>Nothing to map yet</div>
                <p className="mt-1 text-xs text-slate-400">This connection has no scanned inventory or workloads. Scan inventory or create a workload to populate the graph.</p>
                <button onClick={() => navigate("/inventory")} className="mt-3 rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white">Open Inventory →</button>
              </div>
            </div>
          )}
          {expanding && <div className="absolute left-1/2 top-3 z-20 -translate-x-1/2 rounded-full bg-slate-800/90 px-3 py-1 text-xs text-white">Working…</div>}
          {mode !== "explore" && <div className="absolute left-1/2 top-3 z-20 -translate-x-1/2 rounded-full bg-brand/90 px-3 py-1 text-xs text-white">{mode === "path" ? (pathSource ? "Pick target node" : "Pick source node") : "Click a node for blast radius"}</div>}

          <div className={`pointer-events-none absolute bottom-2 left-2 z-10 rounded-md px-2.5 py-1 text-[11px] shadow-sm ${dark ? "bg-slate-800/90 text-slate-300" : "bg-white/90 text-slate-500"}`}>{stats.nodes} nodes · {stats.edges} edges{status ? ` — ${status}` : ""}</div>

          {/* Floating legend (item 24) */}
          <FloatingLegend dark={dark} />

          {/* Zoom control — top-left */}
          {cyReady && (
            <div className="absolute left-2 top-2 z-10">
              <ZoomControl cy={cyRef.current} dark={dark} />
            </div>
          )}

          {/* Minimap — bottom-right */}
          <div className="absolute bottom-2 right-2 z-10">
            <Minimap cy={cyRef.current} />
          </div>

          {/* Quick card (hover) */}
          {quickCard && !ctx && (
            <div ref={cardRef} className="pointer-events-none absolute z-30 max-w-[220px] rounded-md border bg-white px-2.5 py-1.5 text-xs shadow-lg" style={{ left: Math.max(8, Math.min(quickCard.x + 8, (containerRef.current?.clientWidth || 600) - 230)), top: Math.max(8, Math.min(quickCard.y + 8, (containerRef.current?.clientHeight || 600) - cardH - 8)) }}>
              <div className="flex items-center gap-1.5 font-medium text-slate-800">{KIND_META[quickCard.node.kind]?.glyph} {quickCard.node.label}</div>
              <QuickFacts node={quickCard.node} />
            </div>
          )}

          {/* Context menu */}
          {ctx && (
            <div ref={ctxMenuRef} className="absolute z-40 w-56 overflow-hidden rounded-md border bg-white py-1 text-sm shadow-xl" style={{ left: Math.max(8, Math.min(ctx.x, (containerRef.current?.clientWidth || 600) - 232)), top: Math.max(8, Math.min(ctx.y, (containerRef.current?.clientHeight || 600) - ctxMenuH - 8)) }} onMouseLeave={() => setCtx(null)}>
              {ctx.nodeId ? (
                <>
                  <MI label="Inspect" onClick={() => { setSelected(ctx.nodeId!); setCtx(null); }} />
                  <MI label="Expand one hop" onClick={() => void expandNode(ctx.nodeId!)} />
                  {(() => {
                    const n = nodeDataRef.current.get(ctx.nodeId!);
                    return n && azurePortalUrl(n, effectiveTenant) ? (
                      <MI label="Open in Azure Portal ↗" onClick={() => openPortal(ctx.nodeId!)} />
                    ) : null;
                  })()}
                  {ctx.kind === "workload" && <MI label="Focus (overlays + drift)" onClick={() => void focus("workload", ctx.nodeId!.slice(3))} />}
                  {ctx.kind === "subscription" && <MI label="Focus subscription" onClick={() => void focus("subscription", ctx.nodeId!.slice(4))} />}
                  <MI label="Blast radius from here" onClick={() => { void runBlast(ctx.nodeId!); setCtx(null); }} />
                  <MI label="Path: set source" onClick={() => { setMode("path"); setPathSource(ctx.nodeId!); highlightNodes([ctx.nodeId!], "path"); setCtx(null); setStatus("Path: pick a target"); }} />
                  <MI label="Isolate neighborhood" onClick={() => { const cy = cyRef.current!; cy.elements().addClass("dim"); cy.getElementById(ctx.nodeId!).closedNeighborhood().removeClass("dim"); setCtx(null); }} />
                  <MI label="Hide node" onClick={() => { cyRef.current?.getElementById(ctx.nodeId!).remove(); setCtx(null); setSelected(null); }} />
                </>
              ) : (
                <>
                  <MI label="Fit all" onClick={() => { cyRef.current?.fit(undefined, 40); setCtx(null); }} />
                  <MI label="Clear highlights" onClick={() => { clearHighlights(); setCtx(null); }} />
                  <MI label="Reset filters" onClick={() => { setHidden(new Set()); applyHidden(new Set()); setCtx(null); }} />
                  <MI label="Hierarchy layout" onClick={() => { runLayout("breadthfirst"); setCtx(null); }} />
                  <MI label="Organic layout" onClick={() => { runLayout("cose"); setCtx(null); }} />
                  <MI label="Export JSON" onClick={() => { exportJson(currentElements()); setCtx(null); }} />
                </>
              )}
            </div>
          )}

          {/* Left side panels */}
          {leftPanel === "analytics" && <AnalyticsPanel connectionId={effectiveConn} onFocus={(id) => { const n = nodeDataRef.current.get(id); if (n) focusNode(n); else { setSelected(id); cyRef.current?.animate({ center: { eles: cyRef.current!.getElementById(id) }, zoom: 1.2 }, { duration: 300 }); } }} onClose={() => setLeftPanel("none")} />}
          {leftPanel === "ask" && <AskPanel connectionId={effectiveConn} scopeKind={focusScope?.kind || "overview"} scopeId={focusScope?.id || ""} onMatched={onAskMatched} onClose={() => setLeftPanel("none")} />}
          {leftPanel === "views" && <ViewsPanel onApply={applyView} onSaveCurrent={saveCurrentView} onClose={() => setLeftPanel("none")} />}

          {/* Inspector */}
          {selected && (
            <GraphInspector
              detail={detailQ.data}
              loading={detailQ.isLoading}
              kind={selKind}
              onClose={() => setSelected(null)}
              onExpand={() => void expandNode(selected)}
              onBlastRadius={() => void runBlast(selected)}
              onPathFrom={() => { setMode("path"); setPathSource(selected); highlightNodes([selected], "path"); setStatus("Path: pick a target node"); }}
              onPathTo={() => { if (pathSource) void runPath(pathSource, selected); else { setMode("path"); setStatus("Path: pick a source first"); } }}
              onDrift={() => { setDriftMode(true); void focus("workload", selected.slice(3)); }}
              onWarRoom={() => {
                const wid = selected.slice(3);
                try { sessionStorage.setItem("aznet.warRoomHandoff", JSON.stringify({ workloadId: wid, prompt: `Investigate workload ${nodeDataRef.current.get(selected)?.label || wid} from the estate graph.` })); } catch { /* ignore */ }
                navigate("/chat");
              }}
            />
          )}

          {/* Tour bubble */}
          {tourStep >= 0 && TOUR[tourStep] && (
            <div className="absolute bottom-6 left-1/2 z-40 w-96 -translate-x-1/2 rounded-lg border bg-white p-3 shadow-xl">
              <div className="text-sm text-slate-700">{TOUR[tourStep].text}</div>
              <div className="mt-2 flex justify-between">
                <button onClick={() => setTourStep(-1)} className="text-xs text-slate-400 hover:text-slate-600">Skip</button>
                <button onClick={() => setTourStep((s) => s + 1)} className="rounded-md bg-slate-800 px-3 py-1 text-xs text-white">{tourStep + 1 >= TOUR.length ? "Done" : "Next"}</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function MI({ label, onClick }: { label: string; onClick: () => void }) {
  return <button onClick={onClick} className="block w-full px-3 py-1.5 text-left hover:bg-slate-50">{label}</button>;
}

function ToolBtn({ children, onClick, active, dark }: { children: React.ReactNode; onClick: () => void; active?: boolean; dark?: boolean }) {
  const cls = active
    ? "border-brand bg-brand/10 text-brand"
    : dark
      ? "border-slate-600 text-slate-300 hover:bg-slate-700"
      : "border-slate-300 hover:bg-slate-50";
  return <button onClick={onClick} className={`rounded-md border px-2 py-1.5 text-xs ${cls}`}>{children}</button>;
}

// Square icon button with a disabled state — used for the undo/redo history controls.
function IconBtn({ children, onClick, disabled, title, dark }: { children: React.ReactNode; onClick: () => void; disabled?: boolean; title?: string; dark?: boolean }) {
  const base = dark ? "border-slate-600 text-slate-300" : "border-slate-300 text-slate-600";
  const hover = disabled ? "" : dark ? "hover:bg-slate-700" : "hover:bg-slate-50";
  return (
    <button onClick={disabled ? undefined : onClick} disabled={disabled} title={title} aria-label={title}
      className={`rounded-md border px-2 py-1.5 text-sm leading-none ${base} ${hover} ${disabled ? "opacity-40" : ""}`}>{children}</button>
  );
}

// Floating legend bottom-left of the canvas — a compact, collapsible key of the node kinds.
function FloatingLegend({ dark }: { dark: boolean }) {
  const [open, setOpen] = useState(false);
  const KEY: GraphNodeKind[] = ["workload", "resource", "subscription", "architecture", "assessment_finding"];
  return (
    <div className={`absolute bottom-2 left-2 z-10 ${open ? "" : "pointer-events-auto"}`} style={{ marginBottom: 26 }}>
      {open ? (
        <div className={`rounded-md border px-2.5 py-2 text-[11px] shadow-sm ${dark ? "border-slate-700 bg-slate-800/95 text-slate-200" : "border-slate-200 bg-white/95"}`} onMouseLeave={() => setOpen(false)}>
          <div className="mb-1 flex items-center justify-between gap-3">
            <span className="font-semibold uppercase tracking-wide text-slate-400">Legend</span>
            <button onClick={() => setOpen(false)} className="text-slate-400 hover:text-slate-600">✕</button>
          </div>
          {KEY.map((k) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="inline-flex h-4 w-4 items-center justify-center rounded bg-white bg-contain bg-center bg-no-repeat" style={{ border: `1.5px solid ${KIND_META[k].color}`, backgroundImage: `url("${kindIconUri(k)}")`, backgroundSize: "70%" }} />
              <span className={dark ? "text-slate-300" : "text-slate-600"}>{KIND_META[k].label}</span>
            </div>
          ))}
        </div>
      ) : (
        <button onClick={() => setOpen(true)} className={`rounded-md border px-2 py-1 text-[11px] shadow-sm ${dark ? "border-slate-700 bg-slate-800/90 text-slate-300" : "border-slate-200 bg-white/90 text-slate-500"}`} title="Show legend">⊞ Legend</button>
      )}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return <div className="flex items-center gap-2 text-xs"><span className="inline-block h-3 w-3 rounded-full" style={{ backgroundColor: color }} /><span className="text-slate-700">{label}</span></div>;
}

function QuickFacts({ node }: { node: GraphNode }) {
  const d = node.data || {};
  if (node.kind === "workload") return <div className="mt-0.5 text-[11px] text-slate-500">{[d.criticality, d.environment, d.risk?.failed ? `${d.risk.failed} failing` : ""].filter(Boolean).join(" · ")}</div>;
  if (node.kind === "resource") return <div className="mt-0.5 text-[11px] text-slate-500">{[d.short_type, d.location, d.drift].filter(Boolean).join(" · ")}</div>;
  return <div className="mt-0.5 text-[11px] text-slate-500">{KIND_META[node.kind]?.label}</div>;
}

function exportJson(els: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const blob = new Blob([JSON.stringify(els, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "estate-graph.json"; a.click();
  URL.revokeObjectURL(url);
}

// Export the current canvas as a high-res PNG (item 30).
function exportPng(cy: Core | null, dark: boolean) {
  if (!cy) return;
  try {
    const png = cy.png({ full: true, scale: 2, bg: dark ? "#0f172a" : "#ffffff" });
    const a = document.createElement("a");
    a.href = png; a.download = "estate-graph.png"; a.click();
  } catch { /* ignore */ }
}

/** Build a deep link into the Azure Portal for an Azure-hierarchy node, scoped to the
 * connection's tenant directory so it opens in the right tenant. Returns "" for nodes that
 * aren't real Azure objects (workload/architecture/memory/finding/etc.). */
function azurePortalUrl(node: GraphNode, tenantId: string): string {
  const d = node.data || {};
  // `#@{tenant}/` opens the right directory; without a tenant, fall back to the bare `#`.
  const prefix = tenantId ? `https://portal.azure.com/#@${tenantId}/` : "https://portal.azure.com/#";
  switch (node.kind) {
    case "resource": {
      const arm = d.arm_id || ""; // begins with /subscriptions/...
      return arm ? `${prefix}resource${arm}/overview` : "";
    }
    case "resource_group": {
      const sub = d.subscription_id || "";
      const rg = d.resource_group || "";
      return sub && rg ? `${prefix}resource/subscriptions/${sub}/resourceGroups/${encodeURIComponent(rg)}/overview` : "";
    }
    case "subscription": {
      const sub = d.subscription_id || "";
      return sub ? `${prefix}resource/subscriptions/${sub}/overview` : "";
    }
    case "management_group": {
      const mg = d.management_group_id || "";
      return mg ? `${prefix}resource/providers/Microsoft.Management/managementGroups/${encodeURIComponent(mg)}/overview` : "";
    }
    case "tenant_connection": {
      const t = d.tenant_id || tenantId;
      return t ? `https://portal.azure.com/#@${t}` : "https://portal.azure.com/";
    }
    default:
      return ""; // workloads, architectures, memory, findings, etc. aren't Azure portal objects
  }
}
