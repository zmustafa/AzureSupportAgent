import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { ResponsiveContainer, Sankey } from "recharts";
import { api, type BulkNotificationSimulation, type ManagedAlertRule } from "../../api";
import { formatError } from "../../utils/format";
import { AzureIcon } from "../AzureIcon";

type ScopeParams = { connection_id?: string; workload_id?: string; subscription_id?: string; management_group_id?: string };
type TooltipPathPayload = { title?: string; name?: string; paths?: string[]; path_count?: number; value?: number };
type SankeyHover = TooltipPathPayload & { x: number; y: number };

const FAMILY_OPTIONS: ManagedAlertRule["family"][] = ["metric", "log", "activity", "smart", "prometheus"];
const NODE_COLORS: Record<string, string> = { resource: "#0891b2", alert: "#4f46e5", action_group: "#16a34a", receiver: "#d97706", outcome: "#64748b" };
const SELECTED_COLOR = "#60a5fa";

function armTypeFromId(resourceId: string): string {
  const parts = resourceId.split("/").filter(Boolean);
  const providerIndex = parts.findIndex((part) => part.toLowerCase() === "providers");
  if (providerIndex < 0 || providerIndex + 2 >= parts.length) return "";
  const tail = parts.slice(providerIndex + 1);
  return [tail[0], ...tail.slice(1).filter((_part, index) => index % 2 === 0)].join("/").toLowerCase();
}

type SankeyNodeProps = { selectedKey?: string; highlightedNodeIds?: ReadonlySet<string>; onSelect?: (key: string) => void; onHover?: (item: TooltipPathPayload | null, x?: number, y?: number) => void; [key: string]: unknown };

function SankeyNode(props: SankeyNodeProps) {
  const { x = 0, y = 0, width = 12, height = 10, payload = {}, selectedKey = "", highlightedNodeIds = new Set<string>(), onSelect, onHover } = props as {
    x?: number; y?: number; width?: number; height?: number;
    payload?: { id?: string; name?: string; kind?: string; status?: string; fill?: string; resource_type?: string; paths?: string[]; path_count?: number };
    selectedKey?: string; highlightedNodeIds?: ReadonlySet<string>; onSelect?: (key: string) => void; onHover?: (item: TooltipPathPayload | null, x?: number, y?: number) => void;
  };
  const nodeKey = `node:${payload.id || ""}`;
  const selected = highlightedNodeIds.has(payload.id || "");
  const dimmed = !!selectedKey && !selected;
  const label = String(payload.name || "Unnamed node");
  const short = label.length > 38 ? `${label.slice(0, 35)}…` : label;
  const onRight = ["resource", "alert", "action_group"].includes(payload.kind || "");
  const showAzureIcon = ["resource", "alert", "action_group"].includes(payload.kind || "");
  const iconX = onRight ? x + width + 6 : x - 22;
  const labelX = onRight ? x + width + (showAzureIcon ? 27 : 7) : x - (showAzureIcon ? 27 : 7);
  const anchor = onRight ? "start" : "end";
  const fill = selected ? SELECTED_COLOR : payload.status === "error" ? "#dc2626" : payload.status === "disabled" ? "#94a3b8" : payload.fill || NODE_COLORS[payload.kind || ""] || "#64748b";
  const resourceType = showAzureIcon ? payload.resource_type || "Unknown" : "";
  const hoverText = `${label} · ${(payload.kind || "node").replaceAll("_", " ")} · ${payload.status || "ok"}${resourceType ? ` · Resource type: ${resourceType}` : ""}`;
  return <g role="button" tabIndex={0} aria-label={`Highlight complete paths for ${hoverText}`} onClick={(event) => { event.stopPropagation(); onSelect?.(selectedKey === nodeKey ? "" : nodeKey); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect?.(selectedKey === nodeKey ? "" : nodeKey); } }} onMouseEnter={(event) => onHover?.({ name: label, paths: payload.paths, path_count: payload.path_count }, event.clientX, event.clientY)} onMouseLeave={() => onHover?.(null)} className="cursor-pointer outline-none" opacity={dimmed ? 0.22 : 1}>
    <rect x={x} y={y} width={width} height={Math.max(3, height)} rx={2} fill={fill} fillOpacity={0.9} className="transition-all duration-150" />
    {showAzureIcon && <foreignObject x={iconX} y={y + Math.max(3, height) / 2 - 8} width={16} height={16} style={{ overflow: "visible" }}>
      <div className="h-4 w-4 rounded bg-white/90 p-0.5 shadow-sm"><AzureIcon kind="resource" type={payload.resource_type} className="h-full w-full" /></div>
    </foreignObject>}
    <text x={labelX} y={y + Math.max(10, height) / 2} dy="0.35em" textAnchor={anchor} fontSize={11} fontWeight={600} fill="#334155" stroke="white" strokeWidth={3} paintOrder="stroke">
      {short}
    </text>
  </g>;
}

type SankeyLinkProps = { selectedKey?: string; highlightedKeys?: ReadonlySet<string>; onSelect?: (key: string) => void; onHover?: (item: TooltipPathPayload | null, x?: number, y?: number) => void; [key: string]: unknown };

function SankeyLink(props: SankeyLinkProps) {
  const {
    sourceX = 0, sourceY = 0, targetX = 0, targetY = 0,
    sourceControlX = 0, targetControlX = 0, linkWidth = 1, payload = {},
    selectedKey = "", highlightedKeys = new Set<string>(), onSelect, onHover,
  } = props as {
    sourceX?: number; sourceY?: number; targetX?: number; targetY?: number;
    sourceControlX?: number; targetControlX?: number; linkWidth?: number;
    payload?: { key?: string; status?: string; title?: string; value?: number; paths?: string[]; path_count?: number };
    selectedKey?: string; highlightedKeys?: ReadonlySet<string>; onSelect?: (key: string) => void; onHover?: (item: TooltipPathPayload | null, x?: number, y?: number) => void;
  };
  const key = String(payload.key || "");
  const selected = highlightedKeys.has(key);
  const dimmed = !!selectedKey && !selected;
  const color = selected ? SELECTED_COLOR : payload.status === "error" ? "#dc2626" : payload.status === "disabled" ? "#94a3b8" : "#94a3b8";
  const path = `M${sourceX},${sourceY} C${sourceControlX},${sourceY} ${targetControlX},${targetY} ${targetX},${targetY}`;
  return <g role="button" tabIndex={0} aria-label={`Highlight complete path for ${payload.title || "notification path"}`} onClick={(event) => { event.stopPropagation(); onSelect?.(selectedKey === key ? "" : key); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect?.(selectedKey === key ? "" : key); } }} onMouseEnter={(event) => onHover?.({ title: payload.title, value: payload.value, paths: payload.paths, path_count: payload.path_count }, event.clientX, event.clientY)} onMouseLeave={() => onHover?.(null)} className="cursor-pointer outline-none">
    <path d={path} fill="none" stroke="transparent" strokeWidth={Math.max(12, linkWidth + 8)} />
    <path d={path} fill="none" stroke={color} strokeWidth={Math.max(1, linkWidth)} strokeOpacity={dimmed ? 0.08 : selected ? 0.78 : 0.28} className="transition-all duration-150" />
  </g>;
}

function SankeyPathTooltip({ item }: { item: SankeyHover }) {
  const paths = item.paths || [];
  const total = item.path_count ?? paths.length;
  return <div role="tooltip" className="pointer-events-none fixed z-[70] max-w-[520px] rounded-lg border border-gray-200 bg-white p-3 text-xs shadow-xl" style={{ left: item.x, top: item.y }}>
    <div className="font-semibold text-gray-900">{item.title || item.name || "Notification path"}</div>
    {item.value != null && <div className="mt-0.5 text-[10px] text-gray-500">{item.value} flow{item.value === 1 ? "" : "s"}</div>}
    <div className="mt-2 text-[10px] font-medium uppercase tracking-wide text-gray-400">Complete {total === 1 ? "path" : "paths"}</div>
    <div className="mt-1 space-y-1">{paths.map((path) => <div key={path} className="rounded bg-gray-50 px-2 py-1 leading-4 text-gray-700">{path}</div>)}</div>
    {total > paths.length && <div className="mt-1 text-[10px] text-gray-400">+ {total - paths.length} more complete paths</div>}
  </div>;
}

function Kpi({ label, value, tone = "text-gray-900" }: { label: string; value: number; tone?: string }) {
  return <div className="h-8 w-max min-w-16 flex-none rounded-lg border bg-white px-2 py-px" title={label}><div className={`text-base font-semibold leading-4 tabular-nums ${tone}`}>{value}</div><div className="whitespace-nowrap text-[8px] font-medium uppercase leading-3 tracking-wide text-gray-400">{label}</div></div>;
}

function download(text: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click(); URL.revokeObjectURL(url);
}

function csv(result: BulkNotificationSimulation): string {
  const columns = ["resource_ids", "rule_name", "family", "severity", "rule_enabled", "action_group_name", "receiver_type", "receiver_name", "receiver_destination", "payload_schema", "outcome", "issues"];
  const quote = (value: unknown) => {
    const raw = String(value ?? "");
    const stripped = raw.trimStart();
    const safe = stripped && "=+-@".includes(stripped[0]) ? `'${raw}` : raw;
    return `"${safe.replaceAll('"', '""')}"`;
  };
  return [columns.join(","), ...result.routes.map((row) => columns.map((key) => quote(key === "resource_ids" ? row.resource_ids.join(" | ") : key === "issues" ? row.issues.join(" | ") : key === "receiver_destination" ? row.receiver_destination || row.receiver_masked : row[key as keyof typeof row])).join(","))].join("\n");
}

export function BulkPathSimulator({ params }: { params: ScopeParams }) {
  const [result, setResult] = useState<BulkNotificationSimulation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [family, setFamily] = useState<"all" | ManagedAlertRule["family"]>("all");
  const [severity, setSeverity] = useState("all");
  const [includeDisabled, setIncludeDisabled] = useState(true);
  const [condition, setCondition] = useState<"Fired" | "Resolved">("Fired");
  const [outcome, setOutcome] = useState("all");
  const [query, setQuery] = useState("");
  const [flowQuery, setFlowQuery] = useState("");
  const [maxLinks, setMaxLinks] = useState(250);
  const [page, setPage] = useState(1);
  const [selectedLink, setSelectedLink] = useState("");
  const [hoveredPath, setHoveredPath] = useState<SankeyHover | null>(null);
  const tooltipTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoRunScope = useRef("");

  useEffect(() => () => {
    if (tooltipTimer.current) clearTimeout(tooltipTimer.current);
  }, []);

  const showPathTooltip = (item: TooltipPathPayload | null, x = 0, y = 0) => {
    if (tooltipTimer.current) clearTimeout(tooltipTimer.current);
    tooltipTimer.current = null;
    setHoveredPath(null);
    if (!item) return;
    tooltipTimer.current = setTimeout(() => {
      setHoveredPath({
        ...item,
        x: Math.max(8, Math.min(x + 12, window.innerWidth - 540)),
        y: Math.max(8, Math.min(y + 12, window.innerHeight - 220)),
      });
      tooltipTimer.current = null;
    }, 2000);
  };

  async function run() {
    setBusy(true); setError(""); setPage(1); setSelectedLink("");
    try {
      setResult(await api.bulkSimulateNotificationPaths({ ...params, monitor_condition: condition, include_disabled: includeDisabled, families: family === "all" ? [] : [family], severities: severity === "all" ? [] : [Number(severity)] }));
    } catch (cause) { setError(formatError(cause)); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    const scopeKey = [params.connection_id, params.workload_id, params.subscription_id, params.management_group_id].map((value) => value || "").join("|");
    if (!scopeKey.replaceAll("|", "") || autoRunScope.current === scopeKey) return;
    autoRunScope.current = scopeKey;
    void run();
  }, [params.connection_id, params.workload_id, params.subscription_id, params.management_group_id]);

  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const deferredFlowQuery = useDeferredValue(flowQuery.trim().toLowerCase());
  const searchableRoutes = useMemo(() => (result?.routes ?? []).map((route) => ({
    route,
    text: `${route.rule_name} ${route.action_group_name} ${route.receiver_type} ${route.receiver_destination || route.receiver_masked} ${route.resource_ids.join(" ")}`.toLowerCase(),
  })), [result]);
  const searchableNodes = useMemo(() => (result?.nodes ?? []).map((node) => ({
    node,
    text: `${node.name} ${node.kind} ${node.status} ${node.resource_id || ""} ${node.family || ""} ${node.receiver_type || ""}`.toLowerCase(),
  })), [result]);
  const filteredRoutes = useMemo(() => {
    return searchableRoutes.filter(({ route, text }) => (outcome === "all" || route.outcome === outcome) && (!deferredQuery || text.includes(deferredQuery))).map(({ route }) => route);
  }, [searchableRoutes, outcome, deferredQuery]);
  const sankeyData = useMemo(() => {
    if (!result) return { nodes: [], links: [] };
    const allLinks = result.links.slice().sort((a, b) => b.value - a.value);
    const tokens = deferredFlowQuery.split(/\s+/).filter(Boolean);
    let matchingLinkKeys: Set<string> | null = null;
    if (tokens.length) {
      const tokenLinkSets: Set<string>[] = [];
      const outgoing = new Map<string, typeof allLinks>();
      const incoming = new Map<string, typeof allLinks>();
      for (const link of allLinks) {
        outgoing.set(link.source, [...(outgoing.get(link.source) || []), link]);
        incoming.set(link.target, [...(incoming.get(link.target) || []), link]);
      }
      const connectedLinks = (nodeIds: Set<string>) => {
        const keys = new Set<string>();
        const upstreamVisited = new Set(nodeIds);
        const upstreamQueue = [...nodeIds];
        while (upstreamQueue.length) {
          const nodeId = upstreamQueue.shift()!;
          for (const link of incoming.get(nodeId) || []) {
            const key = `${link.source}|${link.target}|${link.status}`;
            keys.add(key);
            if (!upstreamVisited.has(link.source)) { upstreamVisited.add(link.source); upstreamQueue.push(link.source); }
          }
        }
        const downstreamVisited = new Set(nodeIds);
        const downstreamQueue = [...nodeIds];
        while (downstreamQueue.length) {
          const nodeId = downstreamQueue.shift()!;
          for (const link of outgoing.get(nodeId) || []) {
            const key = `${link.source}|${link.target}|${link.status}`;
            keys.add(key);
            if (!downstreamVisited.has(link.target)) { downstreamVisited.add(link.target); downstreamQueue.push(link.target); }
          }
        }
        return keys;
      };
      for (const token of tokens) {
        const matchingNodes = new Set(searchableNodes.filter(({ text }) => text.includes(token)).map(({ node }) => node.id));
        tokenLinkSets.push(connectedLinks(matchingNodes));
      }
      matchingLinkKeys = new Set(tokenLinkSets[0] || []);
      for (const key of [...matchingLinkKeys]) {
        if (tokenLinkSets.some((tokenLinks) => !tokenLinks.has(key))) matchingLinkKeys.delete(key);
      }
    }
    const links = allLinks.filter((link) => !matchingLinkKeys || matchingLinkKeys.has(`${link.source}|${link.target}|${link.status}`)).slice(0, maxLinks);
    const used = new Set(links.flatMap((link) => [link.source, link.target]));
    const nodes = result.nodes.filter((node) => used.has(node.id));
    const index = new Map(nodes.map((node, i) => [node.id, i]));
    const names = new Map(nodes.map((node) => [node.id, node.name]));
    const visibleLinks = links.filter((link) => index.has(link.source) && index.has(link.target)).map((link) => ({
      source: index.get(link.source)!, target: index.get(link.target)!, source_id: link.source, target_id: link.target,
      value: link.value, status: link.status, key: `${link.source}|${link.target}|${link.status}`,
      title: `${names.get(link.source) || link.source} → ${names.get(link.target) || link.target}`,
    }));
    const outgoing = new Map<string, typeof visibleLinks>();
    const incoming = new Set<string>();
    for (const link of visibleLinks) {
      outgoing.set(link.source_id, [...(outgoing.get(link.source_id) || []), link]);
      incoming.add(link.target_id);
    }
    const nodePaths = new Map<string, Set<string>>();
    const linkPaths = new Map<string, Set<string>>();
    let enumerated = 0;
    const recordPath = (nodeIds: string[], linkKeys: string[]) => {
      const label = nodeIds.map((id) => names.get(id) || id).join(" → ");
      for (const id of nodeIds) {
        const values = nodePaths.get(id) || new Set<string>(); values.add(label); nodePaths.set(id, values);
      }
      for (const key of linkKeys) {
        const values = linkPaths.get(key) || new Set<string>(); values.add(label); linkPaths.set(key, values);
      }
    };
    const walk = (nodeId: string, nodeIds: string[], linkKeys: string[], visited: Set<string>, branchStatus = "") => {
      if (enumerated >= 2000) return;
      const next = outgoing.get(nodeId) || [];
      if (!next.length) { enumerated += 1; recordPath(nodeIds, linkKeys); return; }
      const matching = branchStatus ? next.filter((link) => link.status === branchStatus) : next;
      for (const link of matching.length ? matching : next) {
        if (visited.has(link.target_id)) continue;
        walk(link.target_id, [...nodeIds, link.target_id], [...linkKeys, link.key], new Set([...visited, link.target_id]), link.status);
      }
    };
    for (const node of nodes) {
      if (!incoming.has(node.id)) walk(node.id, [node.id], [], new Set([node.id]));
    }
    return {
      nodes: nodes.map((node) => ({
        id: node.id, name: node.name, kind: node.kind, status: node.status, fill: NODE_COLORS[node.kind] ?? "#64748b",
        resource_type: node.kind === "resource" ? armTypeFromId(node.resource_id || "") : node.kind === "alert" ? ({ metric: "microsoft.insights/metricalerts", log: "microsoft.insights/scheduledqueryrules", activity: "microsoft.insights/activitylogalerts", smart: "microsoft.alertsmanagement/smartdetectoralertrules", prometheus: "microsoft.alertsmanagement/prometheusrulegroups" }[node.family || ""] || "microsoft.insights/metricalerts") : node.kind === "action_group" ? "microsoft.insights/actiongroups" : "",
        paths: [...(nodePaths.get(node.id) || [])].slice(0, 3), path_count: nodePaths.get(node.id)?.size || 0,
      })),
      links: visibleLinks.map((link) => ({ ...link, paths: [...(linkPaths.get(link.key) || [])].slice(0, 3), path_count: linkPaths.get(link.key)?.size || 0 })),
    };
  }, [result, searchableNodes, maxLinks, deferredFlowQuery]);
  const highlightedLinkKeys = useMemo(() => {
    const highlighted = new Set<string>();
    if (!selectedLink) return highlighted;
    if (selectedLink.startsWith("node:")) {
      const nodeId = selectedLink.slice(5);
      const visitUpstream = (currentId: string) => {
        for (const link of sankeyData.links) {
          if (link.target_id !== currentId || highlighted.has(link.key)) continue;
          highlighted.add(link.key);
          visitUpstream(link.source_id);
        }
      };
      const visitDownstream = (currentId: string) => {
        for (const link of sankeyData.links) {
          if (link.source_id !== currentId || highlighted.has(link.key)) continue;
          highlighted.add(link.key);
          visitDownstream(link.target_id);
        }
      };
      visitUpstream(nodeId);
      visitDownstream(nodeId);
      return highlighted;
    }
    const selected = sankeyData.links.find((link) => link.key === selectedLink);
    if (!selected) return highlighted;
    highlighted.add(selected.key);
    const followsSelectedBranch = (link: (typeof sankeyData.links)[number]) =>
      link.status === selected.status || (link.target_id.startsWith("alert:") && link.status === "ok");
    const visitUpstream = (nodeId: string) => {
      for (const link of sankeyData.links) {
        if (link.target_id !== nodeId || highlighted.has(link.key) || !followsSelectedBranch(link)) continue;
        highlighted.add(link.key);
        visitUpstream(link.source_id);
      }
    };
    const visitDownstream = (nodeId: string) => {
      for (const link of sankeyData.links) {
        if (link.source_id !== nodeId || highlighted.has(link.key) || link.status !== selected.status) continue;
        highlighted.add(link.key);
        visitDownstream(link.target_id);
      }
    };
    visitUpstream(selected.source_id);
    visitDownstream(selected.target_id);
    return highlighted;
  }, [sankeyData, selectedLink]);
  const highlightedNodeIds = useMemo(() => {
    const highlighted = new Set<string>();
    for (const link of sankeyData.links) {
      if (!highlightedLinkKeys.has(link.key)) continue;
      highlighted.add(link.source_id);
      highlighted.add(link.target_id);
    }
    if (selectedLink.startsWith("node:")) highlighted.add(selectedLink.slice(5));
    return highlighted;
  }, [sankeyData, highlightedLinkKeys, selectedLink]);
  const pageCount = Math.max(1, Math.ceil(filteredRoutes.length / 100));
  const currentPage = Math.min(page, pageCount);
  const visibleRoutes = filteredRoutes.slice((currentPage - 1) * 100, currentPage * 100);

  return <div className="space-y-3">
    <section className="rounded-xl border bg-white p-3">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1"><h2 className="text-base font-semibold leading-5 text-gray-900">Fleet notification path simulator</h2><p className="text-xs leading-4 text-gray-500">Dry-run every alert in this scope and trace Resource → Alert → Action Group → Receiver → Outcome. No alerts or notifications are sent.</p></div>
        <button onClick={() => void run()} disabled={busy} className="shrink-0 rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{busy ? "Building routing graph…" : "▶ Simulate all alerts"}</button>
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-x-2 gap-y-1.5 border-t pt-2">
        <label className="w-32 flex-none text-xs">Rule family<select value={family} onChange={(event) => setFamily(event.target.value as typeof family)} className="mt-0.5 w-full rounded border px-2 py-1"><option value="all">All families</option>{FAMILY_OPTIONS.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="w-28 flex-none text-xs">Severity<select value={severity} onChange={(event) => setSeverity(event.target.value)} className="mt-0.5 w-full rounded border px-2 py-1"><option value="all">All severities</option>{[0,1,2,3,4].map((value) => <option key={value} value={value}>Sev {value}</option>)}</select></label>
        <label className="w-24 flex-none text-xs">Event state<select value={condition} onChange={(event) => setCondition(event.target.value as typeof condition)} className="mt-0.5 w-full rounded border px-2 py-1"><option>Fired</option><option>Resolved</option></select></label>
        <label className="flex h-7 flex-none items-center gap-1.5 whitespace-nowrap text-xs"><input type="checkbox" checked={includeDisabled} onChange={(event) => setIncludeDisabled(event.target.checked)} /> Include disabled rules</label>
        {result && <div className="flex h-7 items-center gap-1.5"><button onClick={() => download(csv(result), "notification-paths.csv", "text/csv")} className="rounded border px-2 py-1 text-xs">CSV</button><button onClick={() => download(JSON.stringify(result, null, 2), "notification-paths.json", "application/json")} className="rounded border px-2 py-1 text-xs">JSON</button></div>}
      </div>
      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">{error}</div>}
    </section>

    {result && <>
      <div className="flex flex-wrap items-center gap-1"><Kpi label="Rules" value={result.summary.rules} /><Kpi label="Resources" value={result.summary.resources} /><Kpi label="Action Groups" value={result.summary.action_groups} /><Kpi label="Receiver paths" value={result.summary.receiver_paths} /><Kpi label="Would deliver" value={result.summary.would_deliver} tone="text-green-600" /><Kpi label="Blocked" value={result.summary.blocked} tone="text-red-600" /><Kpi label="Diagnostics" value={result.summary.diagnostics} tone="text-amber-600" />{result.warning && <div className="ml-auto max-w-xl text-right text-[10px] leading-tight text-gray-500">{result.warning}</div>}</div>

      <section className="overflow-hidden rounded-xl border bg-white"><div className="flex flex-wrap items-center gap-2 border-b px-4 py-3"><div className="mr-auto"><h3 className="font-semibold">Expected notification flow</h3><p className="text-xs text-gray-500">Search in plain text, or click a name, icon, vertical bar, or flow segment to highlight complete paths.</p></div><div className="relative"><input aria-label="Search notification flow" value={flowQuery} onChange={(event) => { setFlowQuery(event.target.value); setSelectedLink(""); }} placeholder="Search resource, alert, group, receiver…" className="w-72 rounded border px-3 py-1.5 pr-8 text-xs" />{flowQuery && <button aria-label="Clear flow search" onClick={() => { setFlowQuery(""); setSelectedLink(""); }} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700">×</button>}</div>{flowQuery && <span className="text-[10px] text-gray-500">{sankeyData.nodes.length} matching nodes · {sankeyData.links.length} links</span>}{selectedLink && <button onClick={() => setSelectedLink("")} className="rounded border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">Clear highlight</button>}<label className="text-xs">Maximum links<select value={maxLinks} onChange={(event) => { setMaxLinks(Number(event.target.value)); setSelectedLink(""); }} className="ml-2 rounded border px-2 py-1">{[100,250,500,1000].map((value) => <option key={value}>{value}</option>)}</select></label></div><div className="h-[580px] min-w-0 p-3">{sankeyData.nodes.length ? <ResponsiveContainer width="100%" height="100%"><Sankey data={sankeyData} node={<SankeyNode selectedKey={selectedLink} highlightedNodeIds={highlightedNodeIds} onSelect={setSelectedLink} onHover={showPathTooltip} />} nodePadding={18} nodeWidth={12} margin={{ top: 10, right: 36, bottom: 10, left: 36 }} link={<SankeyLink selectedKey={selectedLink} highlightedKeys={highlightedLinkKeys} onSelect={setSelectedLink} onHover={showPathTooltip} />} /></ResponsiveContainer> : <div className="flex h-full items-center justify-center text-sm text-gray-400">No notification paths match “{flowQuery}”.</div>}</div>{hoveredPath && <SankeyPathTooltip item={hoveredPath} />}<div className="flex flex-wrap justify-center gap-3 border-t px-4 py-2 text-[11px]">{Object.entries(NODE_COLORS).map(([kind,color]) => <span key={kind} className="flex items-center gap-1 capitalize"><span className="h-2.5 w-2.5 rounded" style={{backgroundColor:color}} />{kind.replaceAll("_"," ")}</span>)}</div></section>

      <section className="overflow-hidden rounded-xl border bg-white"><div className="flex flex-wrap items-center gap-2 border-b px-4 py-3"><h3 className="mr-auto font-semibold">Notification routes <span className="text-xs font-normal text-gray-500">({filteredRoutes.length})</span></h3><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="Search resource, rule, group, receiver…" className="w-72 rounded border px-2 py-1.5 text-xs" /><select value={outcome} onChange={(event) => { setOutcome(event.target.value); setPage(1); }} className="rounded border px-2 py-1.5 text-xs"><option value="all">All outcomes</option><option value="deliver">Expected delivery</option><option value="disabled">Disabled</option><option value="missing_group">Missing group</option><option value="no_receiver">No receiver</option></select></div><div className="overflow-auto"><table className="w-full min-w-[1200px] text-left text-xs"><thead className="bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Resource</th><th>Alert</th><th>Action Group</th><th>Receiver</th><th>Schema</th><th>Outcome</th><th>Issues</th></tr></thead><tbody className="divide-y">{visibleRoutes.map((route,index) => <tr key={`${route.rule_id}:${route.action_group_id}:${route.receiver_fingerprint ?? index}`}><td className="max-w-xs px-3 py-2"><div className="truncate" title={route.resource_ids.join("\n")}>{route.resource_ids.map((id) => id.split("/").pop()).join(", ") || "Unscoped"}</div></td><td><div className="font-medium">{route.rule_name}</div><div className="text-[10px] text-gray-400">{route.family} · Sev {route.severity ?? "—"}</div></td><td>{route.action_group_name || "—"}</td><td><div className="capitalize">{route.receiver_type || "—"}</div><div className="text-[10px] text-gray-400">{route.receiver_destination || route.receiver_masked}</div></td><td>{route.payload_schema || "—"}</td><td><span className={`rounded px-2 py-0.5 ${route.would_run ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{route.outcome.replaceAll("_"," ")}</span></td><td className="text-red-600">{route.issues.join(" · ") || "—"}</td></tr>)}</tbody></table></div>{filteredRoutes.length > 100 && <div className="flex items-center justify-between border-t px-3 py-2 text-xs"><span>Showing {(currentPage-1)*100+1}–{Math.min(currentPage*100,filteredRoutes.length)} of {filteredRoutes.length}</span><div className="flex gap-2"><button disabled={currentPage===1} onClick={() => setPage(currentPage-1)} className="rounded border px-2 py-1 disabled:opacity-40">Previous</button><span>Page {currentPage} of {pageCount}</span><button disabled={currentPage===pageCount} onClick={() => setPage(currentPage+1)} className="rounded border px-2 py-1 disabled:opacity-40">Next</button></div></div>}</section>

      <section className="rounded-xl border bg-white"><div className="border-b px-4 py-3"><h3 className="font-semibold">Routing diagnostics</h3><p className="text-xs text-gray-500">Prioritized delivery risks found across the complete scope.</p></div>{result.diagnostics.length ? <div className="divide-y">{result.diagnostics.map((item,index) => <div key={`${item.code}:${item.rule_id}:${index}`} className="flex items-start gap-3 p-3 text-xs"><span className={`rounded px-2 py-0.5 font-medium ${item.severity === "critical" || item.severity === "high" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>{item.severity}</span><div><div className="font-medium text-gray-800">{item.message}</div><div className="text-gray-400">{item.rule_name || item.receiver || item.action_group_id}</div></div></div>)}</div> : <div className="p-8 text-center text-sm text-green-700">No routing diagnostics found.</div>}</section>
    </>}
  </div>;
}
