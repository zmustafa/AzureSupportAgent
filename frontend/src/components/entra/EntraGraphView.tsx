import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import { api } from "../../api";
import type { EntraEscalation, EntraGraphNode } from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import { CoverageBanner, EntraEmpty } from "./EntraShared";

let _fcoseRegistered = false;
if (!_fcoseRegistered) {
  try { cytoscape.use(fcose as unknown as cytoscape.Ext); _fcoseRegistered = true; } catch { /* already registered */ }
}

/** Ring colour per identity node kind. Kept local so the Azure estate palette is untouched. */
const KIND_COLOUR: Record<string, string> = {
  entra_user: "#475569",
  entra_guest: "#d97706",
  entra_group: "#0d9488",
  entra_role: "#dc2626",
  entra_app: "#4f46e5",
  service_principal: "#7c3aed",
  managed_identity: "#0891b2",
  oauth_permission: "#ea580c",
  ca_policy: "#059669",
  entra_tenant: "#4f46e5",
  // Violet, matching the identity-fabric chips: the same external provider, drawn as the
  // node it behaves like.
  federated_domain: "#7c3aed",
};

const KIND_LABEL: Record<string, string> = {
  entra_user: "User",
  entra_guest: "Guest",
  entra_group: "Group",
  entra_role: "Directory role",
  entra_app: "Application",
  service_principal: "Service principal",
  managed_identity: "Managed identity",
  oauth_permission: "Permission",
  ca_policy: "CA policy",
  entra_tenant: "Tenant",
  federated_domain: "Federated domain",
};

const EDGE_COLOUR: Record<string, string> = {
  member_of: "#94a3b8",
  owns: "#0d9488",
  active_in: "#dc2626",
  eligible_for: "#f59e0b",
  granted: "#ea580c",
  protected_by: "#059669",
  excluded_from: "#b45309",
  escalates_to: "#dc2626",
  can_access: "#2563eb",
  authenticates: "#7c3aed",
};

type Lens = "none" | "privilege" | "escalation" | "guest" | "risk";

const LENSES: { id: Lens; label: string }[] = [
  { id: "none", label: "Node kind" },
  { id: "privilege", label: "Privilege" },
  { id: "escalation", label: "Escalation path" },
  { id: "guest", label: "Guest vs member" },
  { id: "risk", label: "Application risk" },
];

function ringFor(node: EntraGraphNode, lens: Lens, onEscalation: Set<string>): string {
  const d = node.data || {};
  if (lens === "privilege") {
    if (d.tier === "tier0") return "#dc2626";
    if (d.tier === "tier1") return "#ea580c";
    return d.privileged ? "#f59e0b" : "#cbd5e1";
  }
  if (lens === "escalation") return onEscalation.has(node.id) ? "#dc2626" : "#cbd5e1";
  if (lens === "guest") {
    if (node.kind === "entra_guest") return "#d97706";
    if (node.kind === "entra_user") return "#475569";
    return "#cbd5e1";
  }
  if (lens === "risk") {
    const score = Number(d.risk_score || 0);
    if (!score) return "#cbd5e1";
    return score >= 70 ? "#dc2626" : score >= 40 ? "#f59e0b" : "#16a34a";
  }
  return KIND_COLOUR[node.kind] || "#94a3b8";
}

function Canvas({ nodes, edges, lens, onSelect }: {
  nodes: EntraGraphNode[];
  edges: { id: string; source: string; target: string; kind: string; label: string;
           data?: Record<string, any> }[];
  lens: Lens;
  onSelect: (node: EntraGraphNode | null) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const [zoom, setZoom] = useState(1);
  const onEscalation = useMemo(() => {
    const s = new Set<string>();
    for (const e of edges) if (e.kind === "escalates_to") { s.add(e.source); s.add(e.target); }
    return s;
  }, [edges]);

  useEffect(() => {
    if (!ref.current) return;
    const cy = cytoscape({
      container: ref.current,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#ffffff",
            "border-width": 3,
            "border-color": "data(ring)",
            label: "data(label)",
            "font-size": 9,
            "text-valign": "bottom",
            "text-margin-y": 4,
            "text-max-width": "110px",
            "text-wrap": "ellipsis",
            color: "#334155",
            width: 26,
            height: 26,
          },
        },
        { selector: 'node[kind="entra_role"]', style: { shape: "diamond", width: 34, height: 34 } },
        { selector: 'node[kind="entra_group"]', style: { shape: "round-rectangle", width: 30, height: 22 } },
        { selector: 'node[kind="oauth_permission"]', style: { shape: "hexagon", width: 20, height: 20, "font-size": 8 } },
        { selector: 'node[kind="ca_policy"]', style: { shape: "round-tag", width: 32, height: 24 } },
        // Deliberately the largest node on the canvas. It is one external system that can
        // issue tokens for everything downstream of it, and it should look like it.
        { selector: 'node[kind="federated_domain"]',
          style: { shape: "cut-rectangle", width: 54, height: 34, "font-size": 11 } },
        {
          selector: "edge",
          style: {
            width: 1.4,
            "line-color": "data(colour)",
            "target-arrow-color": "data(colour)",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "bezier",
            opacity: 0.75,
          },
        },
        {
          selector: 'edge[kind="escalates_to"]',
          style: { "line-style": "dashed", width: 2.2, opacity: 1 },
        },
        {
          selector: 'edge[kind="excluded_from"]',
          style: { "line-style": "dashed" },
        },
        { selector: "node:selected", style: { "border-width": 5 } },
        // Dense graphs (48 app admins × every service principal is a near-complete
        // bipartite graph) turn permanent labels into unreadable mush. Hide them and bring
        // them back on hover/selection, where they are actually being read.
        { selector: "node.quiet", style: { label: "" } },
        {
          selector: "node.hovered, node:selected",
          style: {
            label: "data(label)", "font-size": 12, "z-index": 999,
            "text-background-color": "#ffffff", "text-background-opacity": 0.92,
            "text-background-padding": "3px", "text-max-width": "220px",
            "text-wrap": "ellipsis", color: "#0f172a",
          },
        },
        { selector: "edge.faded", style: { opacity: 0.18 } },
      ],
      // Cytoscape's default is 1. This ran at 0.2, which on a real 400-edge graph meant
      // roughly a dozen wheel notches to cross one zoom step — slow enough that the canvas
      // read as unresponsive. Full speed on the wheel, with buttons for precise steps.
      wheelSensitivity: 1,
    });
    cyRef.current = cy;
    cy.on("tap", "node", (evt) => onSelect((evt.target.data("payload") as EntraGraphNode) || null));
    cy.on("tap", (evt) => { if (evt.target === cy) onSelect(null); });
    cy.on("mouseover", "node", (evt) => evt.target.addClass("hovered"));
    cy.on("mouseout", "node", (evt) => evt.target.removeClass("hovered"));
    cy.on("zoom", () => setZoom(cy.zoom()));
    setZoom(cy.zoom());
    return () => { cy.destroy(); cyRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const present = new Set(nodes.map((n) => n.id));
    const live = edges.filter((e) => present.has(e.source) && present.has(e.target));
    // Fixed repulsion produced a single hairball once a real tenant supplied hundreds of
    // edges: the layout has to loosen as the graph gets denser or nothing is legible.
    const density = live.length / Math.max(nodes.length, 1);
    const dense = nodes.length > 30 || density > 3;
    // A force layout assumes edges pull towards an equilibrium. Escalation data is not
    // like that: it is a fan-out — a few primitives each reaching dozens of principals,
    // 52 nodes and 450 edges on the live tenant. fcose drew that as one illegible ball no
    // matter how far the repulsion was pushed. It is a DAG, so lay it out as one: sources
    // on the left, what they can reach on the right, and the direction of attack readable.
    const hierarchical = live.length > 0
      && live.filter((e) => e.kind === "escalates_to").length / live.length > 0.8;
    cy.elements().remove();
    cy.add([
      ...nodes.map((n) => ({
        group: "nodes" as const,
        data: { id: n.id, kind: n.kind, label: n.label, ring: ringFor(n, lens, onEscalation), payload: n },
        classes: dense ? "quiet" : "",
      })),
      // Belt and braces: the backend already filters, but one orphan edge blanks the whole
      // canvas, so the client refuses to add one too.
      ...live.map((e) => ({
        group: "edges" as const,
        data: { id: e.id, source: e.source, target: e.target, kind: e.kind,
                colour: EDGE_COLOUR[e.kind] || "#94a3b8" },
        classes: dense && e.kind !== "escalates_to" ? "faded" : "",
      })),
    ]);
    cy.layout(
      hierarchical
        ? {
            name: "breadthfirst",
            directed: true,
            grid: true,
            spacingFactor: 1.3,
            avoidOverlap: true,
            animate: false,
            padding: 30,
            // Sources first: a principal that nothing else can reach is where an attack
            // starts, and that is the column a reader should scan.
            roots: nodes
              .filter((n) => !live.some((e) => e.target === n.id))
              .map((n) => n.id),
          }
        : {
            name: nodes.length > 3 ? "fcose" : "grid",
            animate: false,
            nodeRepulsion: dense ? 90000 : 9000,
            idealEdgeLength: dense ? 220 : 90,
            nodeSeparation: dense ? 200 : 75,
            gravity: dense ? 0.05 : 0.25,
            numIter: dense ? 3500 : 2500,
            padding: 30,
          } as cytoscape.LayoutOptions,
    ).run();
    cy.fit(undefined, 40);
  }, [nodes, edges, lens, onEscalation]);

  // Zoom about the centre of the viewport, not the graph's origin: zooming towards a point
  // nobody is looking at throws the thing you were reading off the canvas.
  const zoomBy = useCallback((factor: number) => {
    const cy = cyRef.current;
    if (!cy) return;
    const level = Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor));
    cy.zoom({ level, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, []);

  const fit = useCallback(() => { cyRef.current?.fit(undefined, 40); }, []);

  // Keyboard is the fast path once the canvas has focus: +/- step, 0 fits.
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.3); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomBy(1 / 1.3); }
    else if (e.key === "0") { e.preventDefault(); fit(); }
  }, [zoomBy, fit]);

  return (
    // No wrapper element around the mount on purpose. Cytoscape's column is a flex item
    // carrying min-width:0 so it can shrink below the canvas it already holds; slipping a
    // plain div in between hid that from the flex row and brought the resize flicker back.
    // The controls are a sibling of the mount and position against the column, which the
    // caller marks `relative`.
    <>
      <div
        ref={ref}
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="h-full w-full bg-slate-50 outline-none"
      />
      <div className="absolute right-3 top-3 flex flex-col overflow-hidden rounded-lg border bg-white/95 shadow-sm">
        <button onClick={() => zoomBy(1.3)} title="Zoom in (+)"
                className="px-2 py-1 text-base leading-5 text-gray-600 hover:bg-gray-100">＋</button>
        <div className="border-t px-1 py-0.5 text-center text-[10px] tabular-nums text-gray-500"
             title="Current zoom">
          {Math.round(zoom * 100)}%
        </div>
        <button onClick={() => zoomBy(1 / 1.3)} title="Zoom out (−)"
                className="border-t px-2 py-1 text-base leading-5 text-gray-600 hover:bg-gray-100">－</button>
        <button onClick={fit} title="Fit the whole graph (0)"
                className="border-t px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-100">Fit</button>
      </div>
    </>
  );
}

function Inspector({ node, escalations, onFocus }: {
  node: EntraGraphNode | null;
  escalations: EntraEscalation[];
  onFocus: (kind: string, id: string) => void;
}) {
  if (!node) {
    return (
      <div className="p-3 text-xs text-gray-500">
        Select a node to see what it holds and how it could escalate.
      </div>
    );
  }
  const d = node.data || {};
  const prefix = node.id.split(":")[0];
  const objectId = node.id.slice(prefix.length + 1);
  const related = escalations.filter((e) => e.source === node.id || e.target === node.id);
  return (
    <div className="space-y-3 p-3">
      <div>
        <div className="text-[11px] uppercase tracking-wide text-gray-500">
          {KIND_LABEL[node.kind] || node.kind}
        </div>
        <div className="text-sm font-semibold text-gray-900">{node.label}</div>
        {d.upn && <div className="text-xs text-gray-500">{d.upn}</div>}
      </div>

      <div className="space-y-1 text-[12px]">
        {Object.entries(d)
          .filter(([k, v]) => k !== "upn" && v !== "" && v !== null && v !== undefined)
          .map(([k, v]) => (
            <div key={k} className="flex justify-between gap-2">
              <span className="text-gray-500">{k.replace(/_/g, " ")}</span>
              <span className="text-right text-gray-800">{String(v)}</span>
            </div>
          ))}
      </div>

      {related.length > 0 && (
        <div className="rounded border border-red-200 bg-red-50 p-2">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-red-800">
            Escalation paths
          </div>
          {related.map((e, i) => (
            <div key={i} className="mt-1 text-[12px] text-red-900">
              <div className="font-medium">{e.name}</div>
              <div className="text-red-800">{e.reason}</div>
              {(e.fan_out_total ?? 0) > 12 && (
                <div className="text-red-800">
                  Reaches {e.fan_out_total} target(s) this way — only the first 12 are drawn.
                </div>
              )}
              {e.also_via?.length ? (
                <div className="text-red-800">Also reachable via: {e.also_via.join(", ")}</div>
              ) : null}
              <div className="mt-0.5 text-[10px] italic text-red-700">
                Rule: {e.rule} (confidence: {e.confidence})
              </div>
            </div>
          ))}
        </div>
      )}

      {(prefix === "eu" || prefix === "esp" || prefix === "er") && (
        <button
          className="w-full rounded border px-2 py-1 text-[12px] text-gray-700 hover:bg-gray-50"
          onClick={() => onFocus(
            prefix === "er" ? "role" : prefix === "esp" ? "application" : "principal", objectId)}
        >
          Focus this node
        </button>
      )}
    </div>
  );
}

export function EntraGraphView({ connectionId, onOpenSetup }:
  { connectionId: string | null; onOpenSetup?: () => void }) {
  const [scopeKind, setScopeKind] = useState("privileged");
  const [scopeId, setScopeId] = useState("");
  const [lens, setLens] = useState<Lens>("none");
  const [selected, setSelected] = useState<EntraGraphNode | null>(null);
  const [targetSearch, setTargetSearch] = useState("");
  const [primitive, setPrimitive] = useState<string | null>(null);
  const debouncedSearch = useDebounced(targetSearch, 250);

  const scopesQ = useQuery({ queryKey: ["entra-graph-scopes"], queryFn: () => api.entraGraphScopes() });
  const targetsQ = useQuery({
    queryKey: ["entra-graph-targets", connectionId, debouncedSearch],
    queryFn: () => api.entraGraphTargets(connectionId, debouncedSearch),
  });
  const graphQ = useQuery({
    queryKey: ["entra-graph", connectionId, scopeKind, scopeId],
    queryFn: () => api.entraGraph(scopeKind, scopeId, connectionId),
  });
  const escQ = useQuery({
    queryKey: ["entra-graph-escalations", connectionId],
    queryFn: () => api.entraGraphEscalations(connectionId),
  });

  const targets = useMemo(() => {
    const t = targetsQ.data;
    if (!t) return [] as { id: string; label: string }[];
    if (scopeKind === "principal") return t.principals;
    if (scopeKind === "application") return t.applications;
    if (scopeKind === "role") return t.roles;
    if (scopeKind === "policy") return t.policies;
    return [];
  }, [targetsQ.data, scopeKind]);

  // How many the tenant actually has, versus how many this picker is showing. A plain
  // dropdown capped at a fixed number put most of a 20,000-seat directory out of reach,
  // which quietly made this screen unusable for exactly the tenants that need it.
  const targetTotal = useMemo(() => {
    const t = targetsQ.data;
    if (!t) return 0;
    if (scopeKind === "principal") return t.principal_total ?? t.principals.length;
    if (scopeKind === "application") return t.application_total ?? t.applications.length;
    if (scopeKind === "role") return t.role_total ?? t.roles.length;
    return t.policies.length;
  }, [targetsQ.data, scopeKind]);

  const needsTarget = ["principal", "application", "role", "policy"].includes(scopeKind);

  // 450 escalation edges over 52 nodes is a mesh no layout can make legible, but each edge
  // belongs to exactly one named primitive. Filtering to one turns the mesh into the eight
  // readable sub-graphs it was always made of, and the counts beside each primitive already
  // told the reader those sub-graphs existed.
  const view = useMemo(() => {
    const g = graphQ.data;
    if (!g) return null;
    if (!primitive) return g;
    const edges = g.edges.filter((e) => e.data?.primitive === primitive);
    const touched = new Set(edges.flatMap((e) => [e.source, e.target]));
    return { ...g, nodes: g.nodes.filter((n) => touched.has(n.id)), edges };
  }, [graphQ.data, primitive]);

  if (graphQ.isError) return <div className="p-6 text-sm text-red-600">{formatError(graphQ.error)}</div>;
  const d = graphQ.data;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {d && <CoverageBanner meta={d.meta} onOpenSetup={onOpenSetup} />}

      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b bg-white px-3 py-2">
        <select
          value={scopeKind}
          onChange={(e) => { setScopeKind(e.target.value); setScopeId(""); setSelected(null); }}
          className="rounded border px-2 py-1 text-[13px]"
        >
          {(scopesQ.data?.scopes || []).map((s) => (
            <option key={s.kind} value={s.kind}>{s.label}</option>
          ))}
        </select>
        {needsTarget && (
          <>
            <input
              value={targetSearch}
              onChange={(e) => setTargetSearch(e.target.value)}
              placeholder={`Search ${scopeKind}s\u2026`}
              className="w-44 rounded border px-2 py-1 text-[13px]"
            />
            <select value={scopeId} onChange={(e) => setScopeId(e.target.value)}
                    className="min-w-56 rounded border px-2 py-1 text-[13px]">
              <option value="">Select\u2026</option>
              {targets.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            {targetTotal > targets.length && (
              <span className="text-[11px] text-gray-500">
                {targets.length} of {targetTotal.toLocaleString()} — type to narrow
              </span>
            )}
          </>
        )}
        <span className="ml-auto text-[11px] text-gray-500">Colour by</span>
        <select value={lens} onChange={(e) => setLens(e.target.value as Lens)}
                className="rounded border px-2 py-1 text-[13px]">
          {LENSES.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
      </div>

      {d && (
        <div className="shrink-0 border-b bg-gray-50 px-3 py-1.5 text-[11px] text-gray-600">
          {d.note}
          {d.stats && (
            <span className="ml-2 text-gray-500">
              · {view?.nodes.length ?? d.stats.node_count} node(s),
              {" "}{view?.edges.length ?? d.stats.edge_count} edge(s)
              {primitive && (
                <span className="ml-1 font-medium text-red-700">
                  · filtered to one primitive of {d.stats.edge_count} edge(s)
                </span>
              )}
              {d.truncated && <span className="ml-1 font-medium text-amber-700">· capped for legibility</span>}
              {!primitive
                && (d.stats.node_count > 30 || d.stats.edge_count > 3 * Math.max(d.stats.node_count, 1)) && (
                <span className="ml-1">
                  · names shown on hover — pick a primitive on the right to thin this out
                </span>
              )}
            </span>
          )}
        </div>
      )}

      <div className="flex min-h-0 min-w-0 flex-1">
        {/* min-w-0 is load-bearing. Cytoscape gives its canvases an explicit pixel width,
            and a flex child defaults to min-width:auto, so without this the column refused
            to shrink below the canvas it was already holding. The row then overran its
            parent by exactly one scrollbar width, which produced a horizontal scrollbar,
            which changed the height, which made Cytoscape re-measure — a visible flicker
            that never settled. overflow-hidden keeps a stale canvas size from leaking out
            during the frame between a resize and Cytoscape catching up. */}
        <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
          {graphQ.isLoading && <div className="p-6 text-sm text-gray-500">Building the graph…</div>}
          {d && !d.meta.loaded && <EntraEmpty kind="cold" />}
          {d && d.meta.loaded && !d.nodes.length && (
            <div className="p-6">
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700">
                {needsTarget && !scopeId
                  ? "Choose a target above to build this view."
                  : "Nothing to draw for this scope in the current snapshot."}
              </div>
            </div>
          )}
          {d && d.meta.loaded && d.nodes.length > 0 && (
            <Canvas nodes={view?.nodes ?? d.nodes} edges={view?.edges ?? d.edges}
                    lens={lens} onSelect={setSelected} />
          )}
        </div>
        <div className="w-80 shrink-0 overflow-auto border-l bg-white">
          <Inspector
            node={selected}
            escalations={escQ.data?.escalations || []}
            onFocus={(kind, id) => { setScopeKind(kind); setScopeId(id); setSelected(null); }}
          />
          {!selected && (
            <div className="border-t p-3">
              <div className="flex items-baseline justify-between">
                <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Escalation primitives
                </div>
                {primitive && (
                  <button onClick={() => setPrimitive(null)}
                          className="text-[11px] text-brand underline underline-offset-2">
                    show all
                  </button>
                )}
              </div>
              <div className="mt-1 space-y-2">
                {(escQ.data?.primitives || scopesQ.data?.primitives || []).map((p) => {
                  const count = escQ.data?.by_primitive?.[p.key] ?? 0;
                  const on = primitive === p.key;
                  return (
                    <button
                      key={p.key}
                      type="button"
                      disabled={!count}
                      onClick={() => setPrimitive(on ? null : p.key)}
                      className={`block w-full rounded px-1.5 py-1 text-left text-[11px] ${
                        on ? "bg-red-50 ring-1 ring-red-200"
                          : count ? "hover:bg-gray-50" : "opacity-50"
                      }`}
                    >
                      <div className="font-medium text-gray-800">
                        {p.name}
                        <span className="ml-1 text-gray-400">({count})</span>
                      </div>
                      <div className="text-gray-500">{p.rule}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
