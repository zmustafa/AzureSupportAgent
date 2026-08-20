/**
 * A reusable Sankey flow explorer.
 *
 * Extracted from the Alerts Manager notification-path simulator so that any feature with a
 * chain of related entities can render one. It owns everything that is genuinely generic —
 * link budgeting, complete-path enumeration, connected search, highlight-on-select, zoom, pan,
 * fullscreen and the tooltip — and knows nothing about alerts or backups. Callers supply a
 * graph and the vocabulary to describe it.
 *
 * Two behaviors here are load-bearing and easy to lose in a rewrite:
 *
 * * **Routes are budgeted whole.** When there are more links than the limit, entire
 *   root-to-leaf routes are dropped rather than individual links, because a Sankey that shows
 *   a path stopping halfway reads as "the flow ends here", which is a lie.
 * * **Search expands through connections.** A token matches nodes, then pulls in every link
 *   upstream and downstream of them, and multiple tokens intersect. Searching two names shows
 *   the paths that involve both, instead of two disconnected fragments.
 */
import { useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { Sankey } from "recharts";
import { AzureIcon } from "../AzureIcon";

export type FlowNode = {
  id: string;
  name: string;
  kind: string;
  status?: string;
  /** ARM type used to pick an Azure icon; omit for abstract nodes such as buckets. */
  resource_type?: string;
  /** Free-form payload the caller gets back on selection. */
  meta?: Record<string, unknown>;
};

export type FlowLink = {
  source: string;
  target: string;
  value: number;
  status?: string;
};

type ResolvedNode = FlowNode & { fill: string; paths: string[]; path_count: number; value: number };
type ResolvedLink = FlowLink & {
  source: number; target: number; source_id: string; target_id: string;
  key: string; title: string; paths: string[]; path_count: number;
};
type TooltipPayload = { title?: string; name?: string; paths?: string[]; path_count?: number; value?: number; valueLabel?: string };
type Hover = TooltipPayload & { x: number; y: number };

const ZOOM_MIN = 50;
const ZOOM_MAX = 250;
const ZOOM_STEP = 10;
const SELECTED_COLOR = "#60a5fa";
/** Enumerating every path is exponential; this caps the work on pathological graphs. */
const MAX_ENUMERATED_PATHS = 2000;
const MAX_WALKED_ROUTES = 20_000;

function clampZoom(value: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(value / ZOOM_STEP) * ZOOM_STEP));
}

/**
 * Choose which links to draw when there are more than `maximum`.
 *
 * Walks every complete route, orders them by their heaviest link, then admits routes whole
 * until the budget is spent — so every drawn path runs end to end.
 */
export function completeRouteLinkSelection(allLinks: FlowLink[], maximum: number) {
  if (allLinks.length <= maximum) return { links: allLinks, totalRoutes: 0, shownRoutes: 0, truncated: false };
  const outgoing = new Map<string, FlowLink[]>();
  const incoming = new Set<string>();
  for (const link of allLinks) {
    outgoing.set(link.source, [...(outgoing.get(link.source) || []), link]);
    incoming.add(link.target);
  }
  const roots = [...new Set(allLinks.map((link) => link.source))].filter((id) => !incoming.has(id)).sort();
  const routes: FlowLink[][] = [];
  const walk = (nodeId: string, path: FlowLink[], visited: Set<string>) => {
    const next = outgoing.get(nodeId) || [];
    if (!next.length) { if (path.length) routes.push(path); return; }
    for (const link of next) {
      if (visited.has(link.target)) continue;
      walk(link.target, [...path, link], new Set([...visited, link.target]));
      if (routes.length >= MAX_WALKED_ROUTES) return;
    }
  };
  for (const root of roots) {
    walk(root, [], new Set([root]));
    if (routes.length >= MAX_WALKED_ROUTES) break;
  }
  routes.sort((left, right) =>
    Math.max(...right.map((link) => link.value)) - Math.max(...left.map((link) => link.value))
    || left.map((link) => `${link.source}|${link.target}`).join("").localeCompare(right.map((link) => `${link.source}|${link.target}`).join("")));
  const selected = new Map<string, FlowLink>();
  let shownRoutes = 0;
  for (const route of routes) {
    const additions = route.filter((link) => !selected.has(`${link.source}|${link.target}|${link.status || ""}`));
    if (selected.size + additions.length > maximum) continue;
    for (const link of additions) selected.set(`${link.source}|${link.target}|${link.status || ""}`, link);
    shownRoutes += 1;
  }
  return { links: [...selected.values()], totalRoutes: routes.length, shownRoutes, truncated: selected.size < allLinks.length };
}

function linkKey(link: FlowLink): string {
  return `${link.source}|${link.target}|${link.status || ""}`;
}

// --------------------------------------------------------------------------- renderers
type NodeProps = {
  selectedKey?: string; highlightedNodeIds?: ReadonlySet<string>;
  colors?: Record<string, string>; iconKinds?: ReadonlySet<string>; labelRightKinds?: ReadonlySet<string>;
  showNodeValues?: boolean; formatValue?: (value: number) => string; formatLabel?: (value: number) => string;
  onSelect?: (key: string) => void; onHover?: (item: TooltipPayload | null, x?: number, y?: number) => void;
  [key: string]: unknown;
};

function FlowSankeyNode(props: NodeProps) {
  const {
    x = 0, y = 0, width = 12, height = 10, payload = {}, selectedKey = "",
    highlightedNodeIds = new Set<string>(), colors = {}, iconKinds = new Set<string>(),
    labelRightKinds, showNodeValues, formatValue, formatLabel, onSelect, onHover,
  } = props as {
    x?: number; y?: number; width?: number; height?: number;
    payload?: Partial<ResolvedNode>;
    selectedKey?: string; highlightedNodeIds?: ReadonlySet<string>; colors?: Record<string, string>;
    iconKinds?: ReadonlySet<string>; labelRightKinds?: ReadonlySet<string>;
    showNodeValues?: boolean; formatValue?: (value: number) => string; formatLabel?: (value: number) => string;
    onSelect?: (key: string) => void; onHover?: (item: TooltipPayload | null, x?: number, y?: number) => void;
  };
  const nodeKey = `node:${payload.id || ""}`;
  const selected = highlightedNodeIds.has(payload.id || "");
  const dimmed = !!selectedKey && !selected;
  const label = String(payload.name || "Unnamed node");
  const short = label.length > 38 ? `${label.slice(0, 35)}…` : label;
  // Labels on the leftmost column must be drawn inward: anchoring them outward lets the SVG
  // clip the start of the name, so "ShoppingSite" renders as "ngSite".
  const onRight = labelRightKinds ? labelRightKinds.has(payload.kind || "") : true;
  const showIcon = iconKinds.has(payload.kind || "") && !!payload.resource_type;
  const iconX = onRight ? x + width + 6 : x - 22;
  const labelX = onRight ? x + width + (showIcon ? 27 : 7) : x - (showIcon ? 27 : 7);
  const fill = selected ? SELECTED_COLOR
    : payload.status === "error" ? "#dc2626"
      : payload.status === "warning" ? "#f59e0b"
        : payload.status === "disabled" ? "#94a3b8"
          : colors[payload.kind || ""] || "#64748b";
  const hoverText = `${label} · ${(payload.kind || "node").replaceAll("_", " ")} · ${payload.status || "ok"}`;
  // Reading a weight off a ribbon's thickness is guesswork, so put the number next to the
  // bar as well. Drawn as a second tspan so it can be dimmed without dimming the name.
  const valueLabel = showNodeValues && payload.value != null
    ? (formatLabel ?? formatValue)?.(payload.value) ?? ""
    : "";
  return <g role="button" tabIndex={0} aria-label={`Highlight complete paths for ${hoverText}${valueLabel ? ` · ${valueLabel}` : ""}`}
    onClick={(event) => { event.stopPropagation(); onSelect?.(selectedKey === nodeKey ? "" : nodeKey); }}
    onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect?.(selectedKey === nodeKey ? "" : nodeKey); } }}
    onMouseEnter={(event) => onHover?.({
      name: label, paths: payload.paths, path_count: payload.path_count,
      value: payload.value, valueLabel: formatValue?.(payload.value ?? 0),
    }, event.clientX, event.clientY)}
    onMouseLeave={() => onHover?.(null)}
    className="cursor-pointer outline-none" opacity={dimmed ? 0.22 : 1}>
    <rect x={x} y={y} width={width} height={Math.max(3, height)} rx={2} fill={fill} fillOpacity={0.9} className="transition-all duration-150" />
    {showIcon && <foreignObject x={iconX} y={y + Math.max(3, height) / 2 - 8} width={16} height={16} style={{ overflow: "visible" }}>
      <div className="h-4 w-4 rounded bg-white/90 p-0.5 shadow-sm"><AzureIcon kind="resource" type={payload.resource_type} className="h-full w-full" /></div>
    </foreignObject>}
    <text x={labelX} y={y + Math.max(10, height) / 2} dy="0.35em" textAnchor={onRight ? "start" : "end"}
      fontSize={11} fontWeight={600} fill="#334155" stroke="white" strokeWidth={3} paintOrder="stroke">
      <tspan>{short}</tspan>
      {valueLabel && <tspan fill="#64748b" fontWeight={500}>{`  ${valueLabel}`}</tspan>}
    </text>
  </g>;
}

type LinkProps = {
  selectedKey?: string; highlightedKeys?: ReadonlySet<string>;
  onSelect?: (key: string) => void; onHover?: (item: TooltipPayload | null, x?: number, y?: number) => void;
  formatValue?: (value: number) => string;
  [key: string]: unknown;
};

function FlowSankeyLink(props: LinkProps) {
  const {
    sourceX = 0, sourceY = 0, targetX = 0, targetY = 0, sourceControlX = 0, targetControlX = 0,
    linkWidth = 1, payload = {}, selectedKey = "", highlightedKeys = new Set<string>(),
    onSelect, onHover, formatValue,
  } = props as {
    sourceX?: number; sourceY?: number; targetX?: number; targetY?: number;
    sourceControlX?: number; targetControlX?: number; linkWidth?: number;
    payload?: Partial<ResolvedLink>; selectedKey?: string; highlightedKeys?: ReadonlySet<string>;
    onSelect?: (key: string) => void; onHover?: (item: TooltipPayload | null, x?: number, y?: number) => void;
    formatValue?: (value: number) => string;
  };
  const key = String(payload.key || "");
  const selected = highlightedKeys.has(key);
  const dimmed = !!selectedKey && !selected;
  const color = selected ? SELECTED_COLOR : payload.status === "error" ? "#dc2626" : "#94a3b8";
  const path = `M${sourceX},${sourceY} C${sourceControlX},${sourceY} ${targetControlX},${targetY} ${targetX},${targetY}`;
  return <g role="button" tabIndex={0} aria-label={`Highlight complete path for ${payload.title || "flow"}`}
    onClick={(event) => { event.stopPropagation(); onSelect?.(selectedKey === key ? "" : key); }}
    onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect?.(selectedKey === key ? "" : key); } }}
    onMouseEnter={(event) => onHover?.({
      title: payload.title, value: payload.value, valueLabel: formatValue?.(payload.value ?? 0),
      paths: payload.paths, path_count: payload.path_count,
    }, event.clientX, event.clientY)}
    onMouseLeave={() => onHover?.(null)} className="cursor-pointer outline-none">
    <path d={path} fill="none" stroke="transparent" strokeWidth={Math.max(12, linkWidth + 8)} />
    <path d={path} fill="none" stroke={color} strokeWidth={Math.max(1, linkWidth)}
      strokeOpacity={dimmed ? 0.08 : selected ? 0.78 : 0.28} className="transition-all duration-150" />
  </g>;
}

function PathTooltip({ item }: { item: Hover }) {
  const paths = item.paths || [];
  const total = item.path_count ?? paths.length;
  return <div role="tooltip" className="pointer-events-none fixed z-[70] max-w-[520px] rounded-lg border border-gray-200 bg-white p-3 text-xs shadow-xl" style={{ left: item.x, top: item.y }}>
    <div className="font-semibold text-gray-900">{item.title || item.name || "Flow"}</div>
    {item.valueLabel && <div className="mt-0.5 text-[10px] text-gray-500">{item.valueLabel}</div>}
    <div className="mt-2 text-[10px] font-medium uppercase tracking-wide text-gray-400">Complete {total === 1 ? "path" : "paths"}</div>
    <div className="mt-1 space-y-1">{paths.map((path) => <div key={path} className="rounded bg-gray-50 px-2 py-1 leading-4 text-gray-700">{path}</div>)}</div>
    {total > paths.length && <div className="mt-1 text-[10px] text-gray-400">+ {total - paths.length} more complete paths</div>}
  </div>;
}

// --------------------------------------------------------------------------- explorer
export type SankeyExplorerProps = {
  nodes: FlowNode[];
  links: FlowLink[];
  title: string;
  subtitle?: string;
  /** Node kinds shown with an Azure icon (they must carry `resource_type`). */
  iconKinds?: ReadonlySet<string>;
  /** Node kinds whose label is drawn to the right of the bar. Defaults to all. */
  labelRightKinds?: ReadonlySet<string>;
  colors: Record<string, string>;
  /** Legend entries; defaults to the color map. */
  legend?: [string, string][];
  /** Renders a link's weight in the tooltip, e.g. "12 items" or "€41.20 / month". */
  formatValue?: (value: number) => string;
  /**
   * Shorter form used for the on-chart node labels, which sit next to a name and must not
   * push it off the canvas. Defaults to `formatValue`.
   */
  formatNodeValue?: (value: number) => string;
  /** Persisted zoom bucket, so each feature remembers its own. */
  storageKey: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  onClearFilters?: () => void;
  /** Extra controls in the header row (right of the built-in zoom/fullscreen controls). */
  actions?: ReactNode;
  /** Optional rows rendered between the header and the chart. */
  filterBar?: ReactNode;
  kpiBar?: ReactNode;
  /** Called whenever the selection changes; `null` when cleared. */
  onSelectNode?: (node: (FlowNode & { value: number }) | null) => void;
  /**
   * Print each node's weight next to its label. Worth it when the weight is money — reading
   * a figure off a ribbon's thickness is guesswork — and noise when every node is "1 flow".
   */
  showNodeValues?: boolean;
  maxLinksDefault?: number;
  heightPx?: number;
  /** Fill the height supplied by a flex parent while retaining `heightPx` as a minimum. */
  fillHeight?: boolean;
  /** Optional caller-owned workspace to fullscreen instead of only the explorer section. */
  fullscreenTargetRef?: RefObject<HTMLElement | null>;
  /**
   * Room reserved for the rightmost column's labels. Increase it when the last column has
   * long names and its labels are drawn outward — otherwise the SVG clips them, and drawing
   * them inward instead makes them collide with the previous column.
   */
  marginRight?: number;
};

export function SankeyExplorer({
  nodes, links, title, subtitle, iconKinds = new Set<string>(), labelRightKinds, colors,
  legend, formatValue, formatNodeValue, storageKey, searchPlaceholder = "Search the flow…",
  emptyMessage = "No flows match the selected filters.", onClearFilters, actions, filterBar, kpiBar,
  onSelectNode, maxLinksDefault = 250, heightPx = 580, fillHeight = false, fullscreenTargetRef, marginRight = 36, showNodeValues = false,
}: SankeyExplorerProps) {
  const [flowQuery, setFlowQuery] = useState("");
  const deferredQuery = useDeferredValue(flowQuery.trim().toLowerCase());
  const [maxLinks, setMaxLinks] = useState(maxLinksDefault);
  const [selectedKey, setSelectedKey] = useState("");
  const [hovered, setHovered] = useState<Hover | null>(null);
  const [zoom, setZoom] = useState(() => {
    if (typeof window === "undefined") return 100;
    try {
      const stored = Number(window.localStorage.getItem(storageKey));
      return Number.isFinite(stored) && stored >= ZOOM_MIN && stored <= ZOOM_MAX ? clampZoom(stored) : 100;
    } catch { return 100; }
  });
  const [baseSize, setBaseSize] = useState({ width: 0, height: 0 });
  const [fullscreen, setFullscreen] = useState(false);
  const [panning, setPanning] = useState(false);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const [fitRequest, setFitRequest] = useState(0);
  const tooltipTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sectionRef = useRef<HTMLElement | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const panRef = useRef<{ pointerId: number; x: number; y: number; left: number; top: number; offsetX: number; offsetY: number; moved: boolean } | null>(null);
  const suppressClickRef = useRef(false);

  const clearTooltip = () => {
    if (tooltipTimer.current) clearTimeout(tooltipTimer.current);
    tooltipTimer.current = null;
    setHovered(null);
  };
  useEffect(() => () => { if (tooltipTimer.current) clearTimeout(tooltipTimer.current); }, []);

  const showTooltip = (item: TooltipPayload | null, x = 0, y = 0) => {
    clearTooltip();
    if (!item) return;
    tooltipTimer.current = setTimeout(() => {
      setHovered({
        ...item,
        x: Math.max(8, Math.min(x + 12, window.innerWidth - 540)),
        y: Math.max(8, Math.min(y + 12, window.innerHeight - 220)),
      });
      tooltipTimer.current = null;
    }, 700);
  };

  useEffect(() => {
    try { window.localStorage.setItem(storageKey, String(zoom)); } catch { /* zoom still works */ }
  }, [zoom, storageKey]);

  useEffect(() => {
    const onChange = () => {
      setFullscreen(document.fullscreenElement === (fullscreenTargetRef?.current ?? sectionRef.current));
      clearTooltip();
      window.requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [fullscreenTargetRef]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => {
      const bounds = viewport.getBoundingClientRect();
      setBaseSize({ width: Math.max(1, Math.floor(bounds.width)), height: Math.max(1, Math.floor(bounds.height)) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [nodes.length]);

  // ---- graph projection ------------------------------------------------------
  const graph = useMemo(() => {
    const all = links.slice().sort((a, b) => b.value - a.value || (a.source + a.target).localeCompare(b.source + b.target));
    const tokens = deferredQuery.split(/\s+/).filter(Boolean);
    let matching: Set<string> | null = null;
    if (tokens.length) {
      const outgoing = new Map<string, FlowLink[]>();
      const incoming = new Map<string, FlowLink[]>();
      for (const link of all) {
        outgoing.set(link.source, [...(outgoing.get(link.source) || []), link]);
        incoming.set(link.target, [...(incoming.get(link.target) || []), link]);
      }
      const connected = (nodeIds: Set<string>) => {
        const keys = new Set<string>();
        const up = new Set(nodeIds); const upQueue = [...nodeIds];
        while (upQueue.length) {
          const id = upQueue.shift()!;
          for (const link of incoming.get(id) || []) {
            keys.add(linkKey(link));
            if (!up.has(link.source)) { up.add(link.source); upQueue.push(link.source); }
          }
        }
        const down = new Set(nodeIds); const downQueue = [...nodeIds];
        while (downQueue.length) {
          const id = downQueue.shift()!;
          for (const link of outgoing.get(id) || []) {
            keys.add(linkKey(link));
            if (!down.has(link.target)) { down.add(link.target); downQueue.push(link.target); }
          }
        }
        return keys;
      };
      const perToken = tokens.map((token) => connected(new Set(
        nodes.filter((node) => `${node.name} ${node.kind} ${node.status || ""} ${node.resource_type || ""}`.toLowerCase().includes(token))
          .map((node) => node.id),
      )));
      matching = new Set(perToken[0] || []);
      for (const key of [...matching]) if (perToken.some((set) => !set.has(key))) matching.delete(key);
    }
    const candidates = all.filter((link) => !matching || matching.has(linkKey(link)));
    const selection = completeRouteLinkSelection(candidates, maxLinks);
    const used = new Set(selection.links.flatMap((link) => [link.source, link.target]));
    const visibleNodes = nodes.filter((node) => used.has(node.id));
    const index = new Map(visibleNodes.map((node, i) => [node.id, i]));
    const names = new Map(visibleNodes.map((node) => [node.id, node.name]));
    const visibleLinks = selection.links
      .filter((link) => index.has(link.source) && index.has(link.target))
      .map((link) => ({
        ...link, source: index.get(link.source)!, target: index.get(link.target)!,
        source_id: link.source, target_id: link.target, key: linkKey(link),
        title: `${names.get(link.source) || link.source} → ${names.get(link.target) || link.target}`,
      }));

    const outgoing = new Map<string, typeof visibleLinks>();
    const hasIncoming = new Set<string>();
    for (const link of visibleLinks) {
      outgoing.set(link.source_id, [...(outgoing.get(link.source_id) || []), link]);
      hasIncoming.add(link.target_id);
    }
    const nodePaths = new Map<string, Set<string>>();
    const pathsByLink = new Map<string, Set<string>>();
    let enumerated = 0;
    const record = (ids: string[], keys: string[]) => {
      const label = ids.map((id) => names.get(id) || id).join(" → ");
      for (const id of ids) { const set = nodePaths.get(id) || new Set<string>(); set.add(label); nodePaths.set(id, set); }
      for (const key of keys) { const set = pathsByLink.get(key) || new Set<string>(); set.add(label); pathsByLink.set(key, set); }
    };
    const walk = (id: string, ids: string[], keys: string[], visited: Set<string>) => {
      if (enumerated >= MAX_ENUMERATED_PATHS) return;
      const next = outgoing.get(id) || [];
      if (!next.length) { enumerated += 1; record(ids, keys); return; }
      for (const link of next) {
        if (visited.has(link.target_id)) continue;
        walk(link.target_id, [...ids, link.target_id], [...keys, link.key], new Set([...visited, link.target_id]));
      }
    };
    for (const node of visibleNodes) if (!hasIncoming.has(node.id)) walk(node.id, [node.id], [], new Set([node.id]));

    // A node's weight is what flows into it, except for sources, which have nothing incoming
    // and are measured by what leaves. This is the number the bar's height represents.
    const incomingTotal = new Map<string, number>();
    const outgoingTotal = new Map<string, number>();
    for (const link of visibleLinks) {
      incomingTotal.set(link.target_id, (incomingTotal.get(link.target_id) || 0) + link.value);
      outgoingTotal.set(link.source_id, (outgoingTotal.get(link.source_id) || 0) + link.value);
    }

    return {
      nodes: visibleNodes.map((node) => ({
        ...node, fill: colors[node.kind] ?? "#64748b",
        value: incomingTotal.get(node.id) ?? outgoingTotal.get(node.id) ?? 0,
        paths: [...(nodePaths.get(node.id) || [])].slice(0, 3), path_count: nodePaths.get(node.id)?.size || 0,
      })) as ResolvedNode[],
      links: visibleLinks.map((link) => ({
        ...link, paths: [...(pathsByLink.get(link.key) || [])].slice(0, 3), path_count: pathsByLink.get(link.key)?.size || 0,
      })) as ResolvedLink[],
      truncated: selection.truncated,
      candidateLinkCount: candidates.length,
      shownRoutes: selection.shownRoutes,
      totalRoutes: selection.totalRoutes,
    };
  }, [nodes, links, deferredQuery, maxLinks, colors]);

  /** Size the canvas to the busiest column so nodes never overlap. */
  const requiredHeight = useMemo(() => {
    const minimumHeight = fillHeight ? (baseSize.height || heightPx) : heightPx;
    if (!graph.nodes.length) return minimumHeight;
    const incoming = new Map<string, string[]>();
    for (const link of graph.links) incoming.set(link.target_id, [...(incoming.get(link.target_id) || []), link.source_id]);
    const depths = new Map<string, number>();
    const depthOf = (id: string, visiting = new Set<string>()): number => {
      if (depths.has(id)) return depths.get(id)!;
      if (visiting.has(id)) return 0;
      const parents = incoming.get(id) || [];
      const depth = parents.length ? 1 + Math.max(...parents.map((p) => depthOf(p, new Set([...visiting, id])))) : 0;
      depths.set(id, depth);
      return depth;
    };
    const columns = new Map<number, number>();
    for (const node of graph.nodes) { const depth = depthOf(node.id); columns.set(depth, (columns.get(depth) || 0) + 1); }
    return Math.max(minimumHeight, Math.max(...columns.values(), 1) * 34 + 40);
  }, [graph, heightPx, fillHeight, baseSize.height]);

  const highlightedLinkKeys = useMemo(() => {
    const highlighted = new Set<string>();
    if (!selectedKey) return highlighted;
    const upstream = (id: string) => {
      for (const link of graph.links) {
        if (link.target_id !== id || highlighted.has(link.key)) continue;
        highlighted.add(link.key); upstream(link.source_id);
      }
    };
    const downstream = (id: string) => {
      for (const link of graph.links) {
        if (link.source_id !== id || highlighted.has(link.key)) continue;
        highlighted.add(link.key); downstream(link.target_id);
      }
    };
    if (selectedKey.startsWith("node:")) {
      const id = selectedKey.slice(5);
      upstream(id); downstream(id);
      return highlighted;
    }
    const selected = graph.links.find((link) => link.key === selectedKey);
    if (!selected) return highlighted;
    highlighted.add(selected.key);
    upstream(selected.source_id);
    downstream(selected.target_id);
    return highlighted;
  }, [graph, selectedKey]);

  const highlightedNodeIds = useMemo(() => {
    const highlighted = new Set<string>();
    for (const link of graph.links) {
      if (!highlightedLinkKeys.has(link.key)) continue;
      highlighted.add(link.source_id); highlighted.add(link.target_id);
    }
    if (selectedKey.startsWith("node:")) highlighted.add(selectedKey.slice(5));
    return highlighted;
  }, [graph, highlightedLinkKeys, selectedKey]);

  const select = (key: string) => {
    setSelectedKey(key);
    if (!onSelectNode) return;
    onSelectNode(key.startsWith("node:") ? graph.nodes.find((node) => node.id === key.slice(5)) ?? null : null);
  };

  // ---- zoom / pan ------------------------------------------------------------
  const canPan = baseSize.width * zoom / 100 > baseSize.width + 1 || requiredHeight * zoom / 100 > baseSize.height + 1;

  const changeZoom = (requested: number, focalX?: number, focalY?: number) => {
    const next = clampZoom(requested);
    if (next === zoom) return;
    const viewport = viewportRef.current;
    clearTooltip();
    if (!viewport || !baseSize.width || !baseSize.height) { setZoom(next); return; }
    const oldScale = zoom / 100;
    const focusX = focalX ?? viewport.clientWidth / 2;
    const focusY = focalY ?? viewport.clientHeight / 2;
    const oldOffsetX = Math.max(0, (viewport.clientWidth - baseSize.width * oldScale) / 2);
    const oldOffsetY = Math.max(0, (viewport.clientHeight - baseSize.height * oldScale) / 2);
    const baseX = (viewport.scrollLeft + focusX - oldOffsetX) / oldScale;
    const baseY = (viewport.scrollTop + focusY - oldOffsetY) / oldScale;
    setZoom(next);
    window.requestAnimationFrame(() => {
      const current = viewportRef.current;
      if (!current) return;
      const scale = next / 100;
      const offsetX = Math.max(0, (current.clientWidth - baseSize.width * scale) / 2);
      const offsetY = Math.max(0, (current.clientHeight - baseSize.height * scale) / 2);
      current.scrollTo({ left: offsetX + baseX * scale - focusX, top: offsetY + baseY * scale - focusY });
    });
  };

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      if ((event.deltaY > 0 && zoom <= ZOOM_MIN) || (event.deltaY < 0 && zoom >= ZOOM_MAX)) return;
      event.preventDefault();
      const bounds = viewport.getBoundingClientRect();
      changeZoom(zoom + (event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP), event.clientX - bounds.left, event.clientY - bounds.top);
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, [zoom, baseSize.width, baseSize.height]);

  const fit = () => { clearTooltip(); setPanOffset({ x: 0, y: 0 }); setFitRequest((v) => v + 1); };
  useEffect(() => {
    if (!fitRequest) return;
    const viewport = viewportRef.current;
    if (!viewport || !baseSize.width || !requiredHeight) return;
    const scale = Math.min(viewport.clientWidth / baseSize.width, viewport.clientHeight / requiredHeight);
    setZoom(Math.max(1, Math.min(100, Math.floor(scale * 100))));
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => viewport.scrollTo({ left: 0, top: 0 })));
  }, [fitRequest, baseSize.width, baseSize.height, requiredHeight]);

  const toggleFullscreen = async () => {
    clearTooltip();
    try {
      const target = fullscreenTargetRef?.current ?? sectionRef.current;
      if (document.fullscreenElement === target) await document.exitFullscreen();
      else await target?.requestFullscreen();
    } catch { /* fullscreen is a nicety; failing to enter it must not break the view */ }
  };

  const legendEntries = legend ?? Object.entries(colors);

  const sectionOwnsFullscreen = fullscreen && !fullscreenTargetRef;

  return <section ref={sectionRef} className={`overflow-hidden border bg-white ${sectionOwnsFullscreen ? "flex h-screen w-screen flex-col rounded-none" : fillHeight ? "flex h-full min-h-0 flex-col rounded-xl" : "rounded-xl"}`}>
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-3">
      <div className="mr-auto">
        <h3 className="font-semibold">{title}</h3>
        {subtitle && <p className="text-xs text-gray-500">{subtitle}</p>}
      </div>
      <div className="relative">
        <input aria-label={`Search ${title}`} value={flowQuery}
          onChange={(event) => { setFlowQuery(event.target.value); select(""); }}
          placeholder={searchPlaceholder} className="w-72 rounded border px-3 py-1.5 pr-8 text-xs" />
        {flowQuery && <button aria-label="Clear flow search" onClick={() => { setFlowQuery(""); select(""); }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700">×</button>}
      </div>
      {flowQuery && <span className="text-[10px] text-gray-500">{graph.nodes.length} matching nodes · {graph.links.length} links</span>}
      {selectedKey && <button onClick={() => select("")} className="rounded border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">Clear highlight</button>}
      <label className="text-xs">Maximum links
        <select value={maxLinks} onChange={(event) => { setMaxLinks(Number(event.target.value)); select(""); }} className="ml-2 rounded border px-2 py-1">
          {[100, 250, 500, 1000].map((value) => <option key={value}>{value}</option>)}
        </select>
      </label>
      <div role="group" aria-label="Sankey zoom controls" className="flex items-center overflow-hidden rounded border bg-white text-xs">
        <button type="button" aria-label="Zoom out" title="Zoom out" disabled={zoom <= ZOOM_MIN}
          onClick={() => changeZoom(zoom - ZOOM_STEP)} className="h-7 w-7 border-r font-semibold hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40">−</button>
        <output aria-label="Current Sankey zoom" aria-live="polite" className="w-12 text-center tabular-nums">{zoom}%</output>
        <button type="button" aria-label="Zoom in" title="Zoom in" disabled={zoom >= ZOOM_MAX}
          onClick={() => changeZoom(zoom + ZOOM_STEP)} className="h-7 w-7 border-l font-semibold hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40">+</button>
        <button type="button" aria-label="Fit Sankey chart to viewport" title="Fit chart to viewport" onClick={fit}
          className="h-7 border-l px-2 font-medium hover:bg-gray-50">Fit</button>
      </div>
      <button type="button" aria-label={fullscreen ? "Exit full screen" : "Show full screen"} title={fullscreen ? "Exit full screen" : "Full screen"}
        onClick={() => void toggleFullscreen()} className="h-7 rounded border bg-white px-2.5 text-xs font-medium text-gray-700 hover:bg-gray-50">
        {fullscreen ? "⤢ Exit full screen" : "⛶ Full screen"}
      </button>
      {actions}
    </div>
    {filterBar}
    {kpiBar}
    {graph.truncated && <div role="status" className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
      Link limit reached. Showing {graph.links.length} of {graph.candidateLinkCount} links across {graph.shownRoutes} of {graph.totalRoutes} complete
      routes; partial routes are never drawn. Increase Maximum links to show more.
    </div>}
    <div
      ref={viewportRef}
      tabIndex={0}
      aria-label={`${title} chart, zoom ${zoom}%. Drag anywhere on the chart to pan. Use the mouse wheel, plus, minus, or zero to change zoom.`}
      onPointerDownCapture={(event) => {
        if (event.button !== 0) return;
        const viewport = viewportRef.current;
        if (!viewport) return;
        const target = event.target instanceof Element ? event.target : null;
        const insideChart = !!target?.closest(".recharts-wrapper");
        if (!insideChart && target?.closest("button, a, input, select, textarea, summary, [role='button'], [role='link'], [role='tooltip']")) return;
        clearTooltip();
        suppressClickRef.current = false;
        panRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop, offsetX: panOffset.x, offsetY: panOffset.y, moved: false };
      }}
      onPointerMoveCapture={(event) => {
        const origin = panRef.current;
        const viewport = viewportRef.current;
        if (!origin || !viewport || origin.pointerId !== event.pointerId) return;
        const deltaX = event.clientX - origin.x;
        const deltaY = event.clientY - origin.y;
        if (!origin.moved && Math.hypot(deltaX, deltaY) < 4) return;
        if (!origin.moved) {
          origin.moved = true;
          suppressClickRef.current = true;
          viewport.setPointerCapture(event.pointerId);
          setPanning(true);
        }
        event.preventDefault();
        event.stopPropagation();
        if (canPan) {
          viewport.scrollLeft = origin.left - deltaX;
          viewport.scrollTop = origin.top - deltaY;
        } else {
          const maxX = viewport.clientWidth * 0.45;
          const maxY = viewport.clientHeight * 0.45;
          setPanOffset({
            x: Math.max(-maxX, Math.min(maxX, origin.offsetX + deltaX)),
            y: Math.max(-maxY, Math.min(maxY, origin.offsetY + deltaY)),
          });
        }
      }}
      onPointerUpCapture={(event) => {
        const origin = panRef.current;
        const viewport = viewportRef.current;
        if (!origin || origin.pointerId !== event.pointerId) return;
        if (origin.moved) { event.preventDefault(); event.stopPropagation(); }
        if (viewport?.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
        panRef.current = null;
        setPanning(false);
      }}
      onPointerCancelCapture={() => { panRef.current = null; setPanning(false); }}
      onLostPointerCapture={() => { panRef.current = null; setPanning(false); }}
      onClickCapture={(event) => {
        if (!suppressClickRef.current) return;
        suppressClickRef.current = false;
        event.preventDefault();
        event.stopPropagation();
      }}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (["+", "=", "Add"].includes(event.key)) { event.preventDefault(); changeZoom(zoom + ZOOM_STEP); }
        else if (["-", "_", "Subtract"].includes(event.key)) { event.preventDefault(); changeZoom(zoom - ZOOM_STEP); }
        else if (event.key === "0") { event.preventDefault(); changeZoom(100); }
      }}
      style={fullscreen || fillHeight ? undefined : { height: heightPx }}
      className={`${fullscreen || fillHeight ? "min-h-0 flex-1" : ""} min-w-0 ${canPan ? "overflow-auto" : "overflow-hidden"} outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 ${panning ? "cursor-grabbing select-none" : "cursor-grab"}`}
    >
      {graph.nodes.length ? baseSize.width > 0 && baseSize.height > 0 && (
        <div className="relative" style={{ width: Math.max(baseSize.width, baseSize.width * zoom / 100), height: Math.max(baseSize.height, requiredHeight * zoom / 100) }}>
          <div className="absolute left-1/2 top-1/2" style={{ width: baseSize.width, height: requiredHeight, transform: `translate(calc(-50% + ${panOffset.x}px), calc(-50% + ${panOffset.y}px)) scale(${zoom / 100})`, transformOrigin: "center" }}>
            <Sankey width={baseSize.width} height={requiredHeight} data={graph}
              node={<FlowSankeyNode selectedKey={selectedKey} highlightedNodeIds={highlightedNodeIds} colors={colors} iconKinds={iconKinds} labelRightKinds={labelRightKinds} showNodeValues={showNodeValues} formatValue={formatValue} formatLabel={formatNodeValue ?? formatValue} onSelect={select} onHover={showTooltip} />}
              nodePadding={18} nodeWidth={12} margin={{ top: 12, right: marginRight, bottom: 12, left: 36 }}
              link={<FlowSankeyLink selectedKey={selectedKey} highlightedKeys={highlightedLinkKeys} onSelect={select} onHover={showTooltip} formatValue={formatValue} />} />
          </div>
        </div>
      ) : (
        <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-gray-500">
          <div>{flowQuery ? `No flows match “${flowQuery}”.` : emptyMessage}</div>
          {onClearFilters && <button type="button" onClick={() => { setFlowQuery(""); onClearFilters(); }} className="rounded border px-3 py-1 text-xs text-blue-700">Clear filters</button>}
        </div>
      )}
    </div>
    {hovered && <PathTooltip item={hovered} />}
    <div className="flex shrink-0 flex-wrap justify-center gap-3 border-t px-4 py-2 text-[11px]">
      {legendEntries.map(([kind, color]) => (
        <span key={kind} className="flex items-center gap-1 capitalize">
          <span className="h-2.5 w-2.5 rounded" style={{ backgroundColor: color }} />{kind.replaceAll("_", " ")}
        </span>
      ))}
    </div>
  </section>;
}
