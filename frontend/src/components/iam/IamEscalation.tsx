/** Escalation tab — *"Alice is not an Owner. Can Alice become one?"*
 *
 * A fan-out DAG, not a force system, so the layout is `breadthfirst` with roots = nodes nothing
 * points at. The Entra version tried `fcose` and produced an illegible ball at 52 nodes and 450
 * edges; the reader needs to scan from "where an attack starts" to "full control", and only a
 * layered layout shows that.
 *
 * Two rendering rules carried over verbatim, both of which were production defects:
 *  - the client refuses to add an edge whose endpoints are missing, even though the backend
 *    already filters, because ONE orphan edge makes Cytoscape reject the batch and blank the
 *    whole canvas;
 *  - `limitations` is rendered prominently. An escalation map that could not see managed
 *    identities showing an empty graph reads as "no paths exist", which is the exact opposite
 *    of what it means.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import cytoscape from "cytoscape";
import { api, type IamEscalationGraph, type IamEscalationPath } from "../../api";
import { useIamConnectionId } from "./IamShared";

const KIND_COLOUR: Record<string, string> = {
  principal: "#2563eb",
  identity: "#7c3aed",
  scope: "#0891b2",
  capability: "#dc2626",
};

const CONF_COLOUR: Record<string, string> = {
  high: "#dc2626",
  medium: "#f59e0b",
  low: "#94a3b8",
};

// Below this zoom a 16px node is under 6px and its 9px label is unreadable, so "fit everything"
// stops being a view of the graph and becomes a smear. See the layout block for why.
const MIN_LEGIBLE_ZOOM = 0.35;

function PathRow({ p, onSelect }: { p: IamEscalationPath; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border bg-white p-2">
      <button type="button" onClick={() => { setOpen((v) => !v); onSelect(p.from); }} className="w-full text-left">
        <div className="flex items-baseline gap-2">
          <span className="rounded bg-red-100 px-1.5 text-[10px] font-semibold text-red-800">
            {p.length} hop{p.length === 1 ? "" : "s"}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800">{p.fromLabel}</span>
          <span
            className="rounded px-1 text-[10px] uppercase"
            style={{ color: CONF_COLOUR[p.min_confidence] }}
            title="The weakest link in the chain — a path is only as trustworthy as its least certain hop."
          >
            {p.min_confidence}
          </span>
        </div>
      </button>
      {open && (
        <ol className="mt-1 space-y-0.5 border-t pt-1">
          {p.hops.map((h, i) => (
            <li key={`${h.source}-${h.target}-${i}`} className="text-[11px] text-gray-600">
              <b className="text-gray-800">{h.primitive}</b> → {h.targetLabel}
              <div className="text-gray-400">{h.reason}</div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** How old the cached graph is, and a way to rebuild it without a full Azure refresh. */
function CacheStrip({ connectionId, computing }: { connectionId: string | null; computing: boolean }) {
  const qc = useQueryClient();
  const meta = useQuery({
    queryKey: ["iam", "cache", connectionId ?? ""],
    queryFn: () => api.iamCacheStatus(connectionId),
    staleTime: 30 * 1000,
  });
  const rebuild = useMutation({
    mutationFn: () => api.iamRebuildCache(connectionId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["iam", "escalation"] });
      void qc.invalidateQueries({ queryKey: ["iam", "cache"] });
    },
  });

  const entry = meta.data?.entries.find((e) => e.key === "escalation");
  const built = entry?.generated_at ? new Date(entry.generated_at) : null;
  const stale = entry ? !entry.current : false;

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b bg-white px-3 py-1.5 text-[11px]">
      {!entry || !built ? (
        <span className="text-gray-500">Not cached yet — the first view builds it.</span>
      ) : (
        <>
          <span className={stale ? "text-amber-800" : "text-gray-600"}>
            {stale ? "Cached, but the access data has changed since" : "Cached"} ·{" "}
            {built.toLocaleString()}
          </span>
          <span className="text-gray-400">
            {entry.size?.nodes ?? "—"} nodes, {entry.size?.edges ?? "—"} edges
            {entry.duration_seconds ? ` · built in ${Math.round(entry.duration_seconds)}s` : ""}
          </span>
        </>
      )}
      <button
        type="button"
        onClick={() => rebuild.mutate()}
        disabled={rebuild.isPending || computing}
        className="ml-auto rounded border border-gray-300 px-1.5 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50"
        title="Recompute from the access data already collected. Does not call Azure."
      >
        {rebuild.isPending ? "Rebuilding…" : "↻ Rebuild"}
      </button>
    </div>
  );
}

/** Shown while the graph is actually being computed.
 *
 * A bare "Computing…" for forty seconds is indistinguishable from a hung page. The elapsed
 * counter is this component's own; the expected duration comes from the server and is only
 * shown when the server has a measured build to base it on. */
function ComputingNotice({ connectionId }: { connectionId: string | null }) {
  const [elapsed, setElapsed] = useState(0);
  const meta = useQuery({
    queryKey: ["iam", "cache", connectionId ?? ""],
    queryFn: () => api.iamCacheStatus(connectionId),
    staleTime: 30 * 1000,
  });
  useEffect(() => {
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const typical = meta.data?.entries.find((e) => e.key === "escalation")?.duration_seconds ?? null;
  return (
    <div className="rounded border bg-white p-3">
      <div className="text-sm text-gray-700">
        Computing the escalation graph… <b className="tabular-nums">{elapsed}s</b>
      </div>
      <p className="mt-1 text-[11px] text-gray-500">
        {typical
          ? `This took about ${Math.round(typical)}s last time on this tenant. The result is cached — later visits are instant until the access data changes.`
          : "No previous build to estimate from. The result is cached, so later visits are instant until the access data changes."}
      </p>
      {typical ? (
        <div className="mt-2 h-1 w-full overflow-hidden rounded bg-gray-200">
          <div
            className="h-full bg-sky-400 transition-[width] duration-1000"
            style={{ width: `${Math.min(100, Math.round((elapsed / typical) * 100))}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}

export function EscalationTab() {
  const connectionId = useIamConnectionId();
  const [minConfidence, setMinConfidence] = useState("low");
  const [selected, setSelected] = useState("");
  // Default ON. Rendering every detected capability at once produced ~190 edges between two
  // breadthfirst rows on a real tenant — the same illegible-hairball failure the Entra version
  // hit with fcose, just a different shape. What a reader came for is the routes that actually
  // reach full control; the rest is available but is not the default.
  const [pathsOnly, setPathsOnly] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);

  const q = useQuery({
    queryKey: ["iam", "escalation", minConfidence, connectionId ?? ""],
    queryFn: () => api.iamEscalation({ min_confidence: minConfidence, connection_id: connectionId }),
    staleTime: 60 * 1000,
  });

  const g: IamEscalationGraph | undefined = q.data;

  // Node ids that lie on at least one path to full control.
  const onPath = useMemo(() => {
    const ids = new Set<string>();
    (g?.paths ?? []).forEach((p) => {
      ids.add(p.from);
      p.hops.forEach((h) => { ids.add(h.source); ids.add(h.target); });
    });
    return ids;
  }, [g]);

  const nodes = useMemo(
    () => (g?.nodes ?? []).filter((n) => !pathsOnly || onPath.has(n.id)),
    [g, pathsOnly, onPath],
  );
  const edges = useMemo(
    () => (g?.edges ?? []).filter((e) => !pathsOnly || (onPath.has(e.source) && onPath.has(e.target))),
    [g, pathsOnly, onPath],
  );

  useEffect(() => {
    if (!containerRef.current) return;
    if (!cyRef.current) {
      cyRef.current = cytoscape({
        container: containerRef.current,
        style: [
          {
            selector: "node",
            style: {
              "background-color": "data(colour)",
              label: "data(label)",
              "font-size": "9px",
              color: "#334155",
              "text-valign": "bottom",
              "text-margin-y": 4,
              width: 16,
              height: 16,
            },
          },
          {
            selector: 'node[kind="capability"]',
            style: { width: 30, height: 30, "font-size": "11px", "font-weight": "bold", shape: "diamond" },
          },
          {
            selector: "edge",
            style: {
              width: 1.5,
              "line-color": "data(colour)",
              "target-arrow-color": "data(colour)",
              "target-arrow-shape": "triangle",
              "curve-style": "bezier",
              "arrow-scale": 0.8,
            },
          },
          { selector: ".highlight", style: { "line-color": "#dc2626", width: 3, "z-index": 99 } },
          { selector: "node.dim", style: { opacity: 0.25 } },
          { selector: "edge.dim", style: { opacity: 0.12 } },
        ],
      });
    }
    const cy = cyRef.current;
    const present = new Set(nodes.map((n) => n.id));
    // Belt and braces: the backend already filters, but ONE orphan edge blanks the whole canvas.
    const live = edges.filter((e) => present.has(e.source) && present.has(e.target));

    cy.elements().remove();
    cy.add([
      ...nodes.map((n) => ({
        group: "nodes" as const,
        data: { id: n.id, kind: n.kind, label: n.label, colour: KIND_COLOUR[n.kind] || "#94a3b8" },
      })),
      ...live.map((e) => ({
        group: "edges" as const,
        data: {
          id: e.id,
          source: e.source,
          target: e.target,
          colour: CONF_COLOUR[e.data.confidence] || "#94a3b8",
        },
      })),
    ]);
    if (nodes.length) {
      cy.layout({
        name: "breadthfirst",
        directed: true,
        grid: true,
        spacingFactor: 1.2,
        avoidOverlap: true,
        animate: false,
        padding: 30,
        // Sources first: a principal nothing else can reach is where an attack starts, and that
        // is the column a reader should scan.
        roots: nodes.filter((n) => !live.some((e) => e.target === n.id)).map((n) => n.id),
      } as cytoscape.LayoutOptions).run();
      cy.fit(undefined, 40);
      // An escalation graph is a STAR: every principal converges on Tier 0, so `breadthfirst`
      // puts all of them on one row. Fitting 175 roots into a 950px canvas gave each node 5px —
      // a dotted line and a red smear, with no legible label anywhere. A picture nobody can read
      // is not a smaller picture, it is a different (and false) claim: that there is nothing
      // here to see. Below the legibility floor, stop zooming out and let the reader pan instead;
      // the node/edge counts under the canvas already say how much is off-screen.
      if (cy.zoom() < MIN_LEGIBLE_ZOOM) {
        cy.zoom({ level: MIN_LEGIBLE_ZOOM, renderedPosition: { x: 0, y: 0 } });
        const tier0 = cy.nodes('[kind="capability"]');
        cy.center(tier0.nonempty() ? tier0 : cy.nodes());
      }
    }
  }, [nodes, edges]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("dim highlight");
    if (!selected) return;
    const path = (g?.paths ?? []).find((p) => p.from === selected);
    if (!path) return;
    const ids = new Set<string>([selected]);
    path.hops.forEach((h) => { ids.add(h.source); ids.add(h.target); });
    cy.nodes().forEach((n) => { if (!ids.has(n.id())) n.addClass("dim"); });
    cy.edges().forEach((e) => {
      if (ids.has(e.source().id()) && ids.has(e.target().id())) e.addClass("highlight");
      else e.addClass("dim");
    });
  }, [selected, g]);

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-80 min-h-0 shrink-0 flex-col border-r bg-gray-50">
        <div className="flex items-center gap-2 border-b bg-white px-3 py-2">
          <span className="text-sm font-semibold text-gray-800">Escalation paths</span>
          <select
            value={minConfidence}
            onChange={(e) => setMinConfidence(e.target.value)}
            aria-label="Minimum confidence"
            className="ml-auto rounded border border-gray-300 px-1.5 py-0.5 text-xs"
          >
            <option value="low">All confidence</option>
            <option value="medium">Medium and up</option>
            <option value="high">High only</option>
          </select>
        </div>

        {/* This graph costs tens of seconds to build on a real tenant, so the screen has to say
            whether it is showing a cached result and offer a way to rebuild it. Without that,
            a fast render is indistinguishable from a stale one and the only remedy anyone can
            find is a full Azure refresh. */}
        <CacheStrip connectionId={connectionId} computing={q.isFetching} />
        <div className="min-h-0 flex-1 space-y-2 overflow-auto p-2">
          {q.isLoading && <ComputingNotice connectionId={connectionId} />}

          {/* Rendered BEFORE the paths, and never hidden. An escalation map that could not see
              managed identities showing an empty list reads as an all-clear on exactly the
              thing the reader came to check. */}
          {(g?.limitations?.length ?? 0) > 0 && (
            <div className="rounded border border-amber-300 bg-amber-50 p-2">
              <div className="mb-1 text-[11px] font-semibold text-amber-900">
                What this map cannot see
              </div>
              <ul className="space-y-1">
                {g?.limitations.map((l) => (
                  <li key={l} className="text-[11px] text-amber-900">{l}</li>
                ))}
              </ul>
            </div>
          )}

          {g && g.paths.length === 0 && (
            <div className="rounded border bg-white p-3 text-xs text-gray-600">
              No escalation path to full control was found in what was collected. Check the
              limitations above before reading that as an all-clear.
            </div>
          )}
          {g?.paths.map((p) => (
            <PathRow key={p.from} p={p} onSelect={setSelected} />
          ))}
        </div>
        {g && (
          <div className="border-t bg-white px-3 py-1.5 text-[11px] text-gray-500">
            {pathsOnly ? `${nodes.length} of ${g.stats.node_count}` : g.stats.node_count} nodes ·{" "}
            {pathsOnly ? `${edges.length} of ${g.stats.edge_count}` : g.stats.edge_count} edges
            {g.dropped_edges > 0 && <span className="text-amber-700"> · {g.dropped_edges} dropped</span>}
            {Object.keys(g.fan_out_total ?? {}).length > 0 && (
              <span title="Some sources reach more targets than are drawn; the count is kept honest.">
                {" "}· fan-out capped
              </span>
            )}
          </div>
        )}
      </div>
      <div className="relative min-w-0 flex-1">
        <div ref={containerRef} className="absolute inset-0" />
        <div className="absolute right-2 top-2 flex items-center gap-2">
          <label
            className="flex items-center gap-1 rounded border bg-white px-2 py-1 text-xs text-gray-700 shadow-sm"
            title="Show only the nodes and edges that lie on a route to full control. Off shows every detected capability, which on a large tenant is a hairball."
          >
            <input type="checkbox" checked={pathsOnly} onChange={(e) => setPathsOnly(e.target.checked)} />
            Paths only
          </label>
          {selected && (
            <button
              onClick={() => setSelected("")}
              className="rounded border bg-white px-2 py-1 text-xs text-gray-700 shadow-sm hover:bg-gray-50"
            >
              Clear highlight
            </button>
          )}
        </div>
        {g && nodes.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-gray-500">
            {pathsOnly
              ? "No route to full control was found in what was collected. Untick “Paths only” to see every detected capability, and read the limitations first."
              : "Nothing to draw."}
          </div>
        )}
      </div>
    </div>
  );
}
