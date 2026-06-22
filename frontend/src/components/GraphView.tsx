import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import cytoscape from "cytoscape";
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
  lensColor,
  toElements,
} from "./graph/graphStyle";
import { GraphInspector } from "./graph/GraphInspector";
import { AnalyticsPanel, AskPanel, Minimap, ViewsPanel } from "./graph/GraphPanels";

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

export function GraphPanel() {
  const navigate = useNavigate();
  const { focusId } = useParams<{ focusId?: string }>();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const nodeDataRef = useRef<Map<string, GraphNode>>(new Map());

  const [connectionId, setConnectionId] = usePersistedState<string>("azsup.graph.connection", "");
  const [lens, setLens] = usePersistedState<Lens>("azsup.graph.lens", "none");
  const [hidden, setHidden] = useState<Set<GraphNodeKind>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [ctx, setCtx] = useState<CtxMenu>(null);
  const [quickCard, setQuickCard] = useState<QuickCard>(null);
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
  const [focusScope, setFocusScope] = useState<{ kind: string; id: string } | null>(null);
  const [tourStep, setTourStep] = useState(-1);

  const connQ = useQuery({ queryKey: ["azure-connections"], queryFn: api.azureConnections, staleTime: 60_000 });
  const effectiveConn = useMemo(() => {
    const conns = connQ.data?.connections || [];
    if (connectionId && conns.some((c) => c.id === connectionId)) return connectionId;
    const def = conns.find((c) => c.is_default) || conns[0];
    return def?.id || "";
  }, [connQ.data, connectionId]);

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
        const c = full ? lensColor(l, full) : "";
        if (c) n.data("lensColor", c);
        else n.removeData("lensColor");
      });
    });
  }, []);

  const runLayout = useCallback((name: string) => {
    const cy = cyRef.current;
    if (!cy || cy.elements().length === 0) return;
    const opts: any = { name, animate: true, animationDuration: 400, fit: true, padding: 40 };
    if (name === "breadthfirst") {
      opts.directed = true;
      opts.spacingFactor = 1.1;
      const roots = cy.nodes('[kind = "tenant_connection"]');
      if (roots.nonempty()) opts.roots = roots;
    }
    if (name === "cose") { opts.nodeRepulsion = 8000; opts.idealEdgeLength = 90; opts.randomize = false; }
    cy.layout(opts).run();
  }, []);

  const loadGraph = useCallback((result: GraphResult, layout = "breadthfirst") => {
    const cy = cyRef.current;
    if (!cy) return;
    remember(result.nodes);
    cy.elements().remove();
    cy.add(toElements(result.nodes, result.edges, lens));
    runLayout(layout);
    applyHidden(hidden);
    applyLens(lens);
    setStats({ nodes: result.nodes.length, edges: result.edges.length });
  }, [remember, runLayout, applyHidden, applyLens, hidden, lens]);

  const mergeResult = useCallback((result: GraphResult, sourceId?: string) => {
    const cy = cyRef.current;
    if (!cy) return;
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
      runLayout("cose");
    }
    applyHidden(hidden);
    applyLens(lens);
    setStats({ nodes: cy.nodes().length, edges: cy.edges().length });
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

  const expandNode = useCallback(async (nodeId: string) => {
    setExpanding(true);
    try {
      const result = await api.graphExpand(nodeId, effectiveConn);
      mergeResult(result, nodeId);
      if (result.truncated) setStatus("Showing a capped set of children (large group).");
    } catch (e) { setStatus(formatError(e)); }
    finally { setExpanding(false); setCtx(null); }
  }, [effectiveConn, mergeResult]);

  // -------------------------------------------------- node tap (mode-aware)
  const onNodeTap = useCallback((nodeId: string) => {
    setCtx(null);
    if (mode === "path") {
      if (!pathSource) { setPathSource(nodeId); setStatus("Path: pick a target node"); highlightNodes([nodeId], "path"); }
      else void runPath(pathSource, nodeId);
      return;
    }
    if (mode === "blast") { void runBlast(nodeId); return; }
    setSelected(nodeId);
  }, [mode, pathSource, runPath, runBlast, highlightNodes]);

  // -------------------------------------------------- focus scope (overlays + drift)
  const focus = useCallback(async (kind: string, id: string) => {
    setExpanding(true);
    setSelected(null);
    try {
      const result = await api.graphBuild(kind, id, { connectionId: effectiveConn, overlays: [...overlays], drift: driftMode });
      setFocusScope({ kind, id });
      loadGraph(result, "cose");
      const driftMsg = (result as any).drift?.summary ? ` — ${(result as any).drift.summary}` : "";
      setStatus(`Focused ${kind} · ${result.nodes.length} nodes${driftMsg}`);
    } catch (e) { setStatus(formatError(e)); }
    finally { setExpanding(false); setCtx(null); }
  }, [effectiveConn, overlays, driftMode, loadGraph]);

  const backToOverview = useCallback(() => {
    setFocusScope(null);
    setDriftMode(false);
    if (overviewQ.data) loadGraph(overviewQ.data, "breadthfirst");
  }, [overviewQ.data, loadGraph]);

  // -------------------------------------------------- cytoscape lifecycle
  useEffect(() => {
    if (!containerRef.current || cyRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      style: buildStylesheet(lens),
      minZoom: 0.05,
      maxZoom: 4,
      wheelSensitivity: 0.25,
      boxSelectionEnabled: true,
    });
    cyRef.current = cy;

    cy.on("tap", "node", (evt: EventObject) => onNodeTapRef.current(evt.target.id()));
    cy.on("dbltap", "node", (evt: EventObject) => void expandNodeRef.current(evt.target.id()));
    cy.on("cxttap", "node", (evt: EventObject) => {
      const pos = evt.renderedPosition || { x: 0, y: 0 };
      setCtx({ x: pos.x, y: pos.y, nodeId: evt.target.id(), kind: evt.target.data("kind") });
      setQuickCard(null);
    });
    cy.on("mouseover", "node", (evt: EventObject) => {
      const pos = evt.renderedPosition || { x: 0, y: 0 };
      const full = nodeDataRef.current.get(evt.target.id());
      if (full) setQuickCard({ x: pos.x, y: pos.y, node: full });
    });
    cy.on("mouseout", "node", () => setQuickCard(null));
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

    return () => { cy.destroy(); cyRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Stable refs so the one-time cytoscape handlers always call the latest closures.
  const onNodeTapRef = useRef(onNodeTap);
  const expandNodeRef = useRef(expandNode);
  useEffect(() => { onNodeTapRef.current = onNodeTap; expandNodeRef.current = expandNode; }, [onNodeTap, expandNode]);

  useEffect(() => { cyRef.current?.style(buildStylesheet(lens)); applyLens(lens); }, [lens, applyLens]);

  useEffect(() => {
    if (!overviewQ.data || focusScope) return;
    loadGraph(overviewQ.data, "breadthfirst");
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
  }, [overviewQ.data, focusScope]);

  // Re-focus when overlays/drift change while focused.
  useEffect(() => {
    if (focusScope) void focus(focusScope.kind, focusScope.id);
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
    if (target.empty()) { cy.add(toElements([node], [], lens)); target = cy.getElementById(node.id); runLayout("breadthfirst"); }
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
  }, [remember, clearHighlights, runLayout, lens]);

  // -------------------------------------------------- toggles
  const toggleKind = (k: GraphNodeKind) => {
    setHidden((prev) => { const next = new Set(prev); if (next.has(k)) next.delete(k); else next.add(k); applyHidden(next); return next; });
  };
  const toggleOverlay = (o: string) => setOverlays((prev) => { const next = new Set(prev); if (next.has(o)) next.delete(o); else next.add(o); return next; });

  // -------------------------------------------------- saved views
  const saveCurrentView = useCallback(async (name: string) => {
    const cy = cyRef.current;
    await api.graphSaveView({
      name, connection_id: effectiveConn, scope_kind: focusScope?.kind || "overview", scope_id: focusScope?.id || "",
      lens, layout: "cose", hidden_kinds: [...hidden], overlays: [...overlays],
      camera: cy ? { zoom: cy.zoom(), pan: cy.pan() } : {},
    });
  }, [effectiveConn, focusScope, lens, hidden, overlays]);

  const applyView = useCallback((v: GraphView) => {
    setLens((v.lens as Lens) || "none");
    setHidden(new Set(v.hidden_kinds as GraphNodeKind[]));
    setOverlays(new Set(v.overlays || []));
    if (v.scope_kind === "overview") backToOverview();
    else void focus(v.scope_kind, v.scope_id);
    const cy = cyRef.current;
    if (cy && v.camera?.zoom) setTimeout(() => { cy.zoom(v.camera.zoom); if (v.camera.pan) cy.pan(v.camera.pan); }, 500);
    setLeftPanel("none");
  }, [setLens, backToOverview, focus]);

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
      if (e.key === "f") cyRef.current?.fit(undefined, 40);
      else if (e.key === "Escape") { clearHighlights(); setMode("explore"); setPathSource(null); setSelected(null); setCtx(null); }
      else if (e.key === "b" && selected) void runBlast(selected);
      else if (e.key === "/") { e.preventDefault(); (document.querySelector("input[placeholder^='Search']") as HTMLInputElement)?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, clearHighlights, runBlast]);

  const selKind: GraphNodeKind | "" = (detailQ.data?.node?.kind as GraphNodeKind) || (selected?.startsWith("wl:") ? "workload" : "");

  // -------------------------------------------------- render
  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-50">
      {/* Command bar */}
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
        <div className="flex items-center gap-2"><span className="text-base">🕸️</span><span className="font-semibold text-slate-800">Estate Graph</span></div>
        <div className="relative">
          <input value={searchTerm} onChange={(e) => void runSearch(e.target.value)} placeholder="Search… ( / )" className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand focus:outline-none" />
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
        <select value={lens} onChange={(e) => setLens(e.target.value as Lens)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" title="Lens">
          {LENSES.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
        <select value={effectiveConn} onChange={(e) => setConnectionId(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" title="Azure connection">
          {(connQ.data?.connections || []).map((c) => <option key={c.id} value={c.id}>{c.display_name || c.tenant_id || c.id}{c.is_default ? " (default)" : ""}</option>)}
        </select>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={() => setLeftPanel(leftPanel === "ask" ? "none" : "ask")} className={`rounded-md border px-2 py-1.5 text-xs ${leftPanel === "ask" ? "border-brand bg-brand/5 text-brand" : "border-slate-300 hover:bg-slate-50"}`}>Ask</button>
          <button onClick={() => setLeftPanel(leftPanel === "analytics" ? "none" : "analytics")} className={`rounded-md border px-2 py-1.5 text-xs ${leftPanel === "analytics" ? "border-brand bg-brand/5 text-brand" : "border-slate-300 hover:bg-slate-50"}`}>Analytics</button>
          <button onClick={() => setLeftPanel(leftPanel === "views" ? "none" : "views")} className={`rounded-md border px-2 py-1.5 text-xs ${leftPanel === "views" ? "border-brand bg-brand/5 text-brand" : "border-slate-300 hover:bg-slate-50"}`}>Views</button>
          <button onClick={() => setTourStep(0)} className="rounded-md border border-slate-300 px-2 py-1.5 text-xs hover:bg-slate-50" title="Guided tour">Tour</button>
          <button onClick={() => runLayout("breadthfirst")} className="rounded-md border border-slate-300 px-2 py-1.5 text-xs hover:bg-slate-50">Hierarchy</button>
          <button onClick={() => runLayout("cose")} className="rounded-md border border-slate-300 px-2 py-1.5 text-xs hover:bg-slate-50">Organic</button>
          <button onClick={() => { clearHighlights(); cyRef.current?.fit(undefined, 40); }} className="rounded-md border border-slate-300 px-2 py-1.5 text-xs hover:bg-slate-50">Fit</button>
        </div>
      </div>

      {/* Mode / overlay strip */}
      <div className="flex flex-wrap items-center gap-2 border-b bg-slate-50 px-4 py-1.5 text-xs">
        {focusScope && <button onClick={backToOverview} className="rounded-md bg-slate-800 px-2 py-1 text-white">← Overview</button>}
        <span className="text-slate-400">Mode:</span>
        {(["explore", "path", "blast"] as Mode[]).map((m) => (
          <button key={m} onClick={() => { setMode(m); setPathSource(null); clearHighlights(); }} className={`rounded-md px-2 py-1 capitalize ${mode === m ? "bg-brand text-white" : "border border-slate-300 hover:bg-slate-50"}`}>{m}</button>
        ))}
        <span className="ml-3 text-slate-400">Overlays:</span>
        {OVERLAY_OPTS.map((o) => (
          <button key={o.id} onClick={() => toggleOverlay(o.id)} className={`rounded-md px-2 py-1 ${overlays.has(o.id) ? "bg-emerald-600 text-white" : "border border-slate-300 hover:bg-slate-50"}`}>{o.label}</button>
        ))}
        <button onClick={() => setDriftMode((v) => !v)} className={`ml-3 rounded-md px-2 py-1 ${driftMode ? "bg-violet-600 text-white" : "border border-slate-300 hover:bg-slate-50"}`} title="Intent vs reality">Drift</button>
        {!focusScope && <span className="text-[11px] text-slate-400">(overlays + drift apply when you Focus a workload)</span>}
      </div>

      <div className="relative flex min-h-0 flex-1">
        {/* Left rail */}
        <div className="hidden w-52 shrink-0 flex-col gap-3 overflow-y-auto border-r bg-white px-3 py-3 lg:flex">
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Layers</div>
            <div className="flex flex-col gap-0.5">
              {ALL_KINDS.map((k) => (
                <label key={k} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-slate-50">
                  <input type="checkbox" checked={!hidden.has(k)} onChange={() => toggleKind(k)} />
                  <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: KIND_META[k].color }} />
                  <span className="truncate text-slate-700">{KIND_META[k].label}</span>
                </label>
              ))}
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

        {/* Canvas */}
        <div className="relative min-h-0 flex-1">
          <div ref={containerRef} className="absolute inset-0" />
          {overviewQ.isLoading && <div className="absolute inset-0 flex items-center justify-center text-sm text-slate-500">Loading estate graph…</div>}
          {overviewQ.isError && <div className="absolute inset-0 flex items-center justify-center text-sm text-red-600">{formatError(overviewQ.error)}</div>}
          {expanding && <div className="absolute left-1/2 top-3 z-20 -translate-x-1/2 rounded-full bg-slate-800/90 px-3 py-1 text-xs text-white">Working…</div>}
          {mode !== "explore" && <div className="absolute left-1/2 top-3 z-20 -translate-x-1/2 rounded-full bg-brand/90 px-3 py-1 text-xs text-white">{mode === "path" ? (pathSource ? "Pick target node" : "Pick source node") : "Click a node for blast radius"}</div>}

          <div className="pointer-events-none absolute bottom-2 left-2 z-10 rounded-md bg-white/90 px-2.5 py-1 text-[11px] text-slate-500 shadow-sm">{stats.nodes} nodes · {stats.edges} edges{status ? ` — ${status}` : ""}</div>
          <div className="absolute bottom-2 right-2 z-10"><Minimap cy={cyRef.current} /></div>

          {/* Quick card (hover) */}
          {quickCard && !ctx && (
            <div className="pointer-events-none absolute z-30 max-w-[220px] rounded-md border bg-white px-2.5 py-1.5 text-xs shadow-lg" style={{ left: Math.min(quickCard.x + 8, (containerRef.current?.clientWidth || 600) - 230), top: quickCard.y + 8 }}>
              <div className="flex items-center gap-1.5 font-medium text-slate-800">{KIND_META[quickCard.node.kind]?.glyph} {quickCard.node.label}</div>
              <QuickFacts node={quickCard.node} />
            </div>
          )}

          {/* Context menu */}
          {ctx && (
            <div className="absolute z-40 w-56 overflow-hidden rounded-md border bg-white py-1 text-sm shadow-xl" style={{ left: Math.min(ctx.x, (containerRef.current?.clientWidth || 600) - 230), top: ctx.y }} onMouseLeave={() => setCtx(null)}>
              {ctx.nodeId ? (
                <>
                  <MI label="Inspect" onClick={() => { setSelected(ctx.nodeId!); setCtx(null); }} />
                  <MI label="Expand one hop" onClick={() => void expandNode(ctx.nodeId!)} />
                  {ctx.kind === "workload" && <MI label="Focus (overlays + drift)" onClick={() => void focus("workload", ctx.nodeId!.slice(3))} />}
                  {ctx.kind === "subscription" && <MI label="Focus subscription" onClick={() => void focus("subscription", ctx.nodeId!.slice(4))} />}
                  <MI label="Blast radius from here" onClick={() => void runBlast(ctx.nodeId!)} />
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
