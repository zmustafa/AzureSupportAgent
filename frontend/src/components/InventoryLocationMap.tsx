import { useMemo, useRef, useState } from "react";
import { feature } from "topojson-client";
import type { Topology } from "topojson-specification";
import type { Feature, GeoJsonProperties, Geometry } from "geojson";
import countries110m from "world-atlas/countries-110m.json";
import type { InventoryResource } from "../api";
import { friendlyLocation, friendlyResourceType } from "./AzureIcon";

// --- Azure region → approximate geographic coordinates (lat, lng of the host city) ---
// Used to plot each region on the world map. Unknown regions are simply skipped.
const REGION_COORDS: Record<string, [number, number]> = {
  eastus: [37.37, -79.82], eastus2: [36.85, -78.0], eastus3: [33.75, -84.39],
  eastus2euap: [37.0, -79.0], centraluseuap: [41.6, -93.6],
  southcentralus: [29.42, -98.49], northcentralus: [41.88, -87.63],
  westcentralus: [41.14, -104.82], centralus: [41.59, -93.6],
  westus: [37.78, -122.42], westus2: [47.23, -119.85], westus3: [33.45, -112.07],
  canadacentral: [43.65, -79.38], canadaeast: [46.81, -71.21],
  brazilsouth: [-23.55, -46.63], brazilsoutheast: [-22.9, -43.2],
  mexicocentral: [19.43, -99.13],
  northeurope: [53.34, -6.26], westeurope: [52.37, 4.9],
  uksouth: [51.5, -0.12], ukwest: [51.48, -3.18],
  francecentral: [48.85, 2.35], francesouth: [43.3, 5.37],
  germanywestcentral: [50.11, 8.68], germanynorth: [52.52, 13.4],
  switzerlandnorth: [47.38, 8.54], switzerlandwest: [46.2, 6.14],
  norwayeast: [59.91, 10.75], norwaywest: [58.97, 5.73],
  swedencentral: [60.67, 17.14], swedensouth: [55.6, 13.0],
  polandcentral: [52.23, 21.01], italynorth: [45.46, 9.19],
  spaincentral: [40.42, -3.7], austriaeast: [48.21, 16.37],
  eastasia: [22.32, 114.17], southeastasia: [1.35, 103.82],
  japaneast: [35.68, 139.69], japanwest: [34.69, 135.5],
  australiaeast: [-33.87, 151.21], australiasoutheast: [-37.81, 144.96],
  australiacentral: [-35.28, 149.13], australiacentral2: [-35.3, 149.0],
  centralindia: [18.52, 73.86], southindia: [13.08, 80.27], westindia: [19.08, 72.88],
  jioindiawest: [22.47, 70.06], jioindiacentral: [21.15, 79.09],
  koreacentral: [37.57, 126.98], koreasouth: [35.18, 129.08],
  uaenorth: [25.2, 55.27], uaecentral: [24.45, 54.38],
  qatarcentral: [25.29, 51.53], israelcentral: [32.08, 34.78],
  southafricanorth: [-26.2, 28.05], southafricawest: [-33.92, 18.42],
  indonesiacentral: [-6.2, 106.85], malaysiawest: [3.14, 101.69],
  newzealandnorth: [-36.85, 174.76],
};

// Map canvas. We crop to the latitude band that actually contains Azure regions
// (Norway/Sweden in the north down to NZ/South Africa/Brazil in the south) so there's
// no wasted empty ocean, and the viewBox stays a proper equirectangular aspect ratio
// at any container width.
const W = 1000;
const LAT_TOP = 83;
const LAT_BOTTOM = -56;
const H = Math.round((W * (LAT_TOP - LAT_BOTTOM)) / 360); // ~386

// Equirectangular projection (matches the country geometry below so markers align).
function project(lat: number, lng: number): { x: number; y: number } {
  const x = ((lng + 180) / 360) * W;
  const y = ((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM)) * H;
  return { x, y };
}

// --- Real world map: project Natural Earth country outlines (bundled TopoJSON, offline)
// into our equirectangular space once at module load, producing one SVG path per country. ---
const COUNTRY_PATHS: string[] = (() => {
  try {
    const fc = feature(
      countries110m as unknown as Topology,
      (countries110m as unknown as Topology).objects.countries,
    ) as unknown as { features: Feature<Geometry, GeoJsonProperties>[] };

    const ringToPath = (ring: number[][]): string => {
      let d = "";
      for (let i = 0; i < ring.length; i++) {
        const [lng, lat] = ring[i];
        const { x, y } = project(lat, lng);
        d += `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
      }
      return d + "Z";
    };

    const paths: string[] = [];
    for (const f of fc.features) {
      const g = f.geometry;
      if (!g) continue;
      if (g.type === "Polygon") {
        paths.push(g.coordinates.map(ringToPath).join(""));
      } else if (g.type === "MultiPolygon") {
        paths.push(g.coordinates.map((poly) => poly.map(ringToPath).join("")).join(""));
      }
    }
    return paths;
  } catch {
    return [];
  }
})();

interface RegionPoint {
  key: string;
  name: string;
  count: number;
  x: number;
  y: number;
  workloads: Set<string>;
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 8;

interface View {
  scale: number;
  tx: number;
  ty: number;
}

/** Keep the map within the viewBox so it can't be panned off-screen. */
function clampView(scale: number, tx: number, ty: number): View {
  const sw = W * scale;
  const sh = H * scale;
  const cx = Math.min(0, Math.max(W - sw, tx));
  const cy = Math.min(0, Math.max(H - sh, ty));
  return { scale, tx: cx, ty: cy };
}

/** Rendered scale + centering offsets for a viewBox fit into a (taller) element with
 *  preserveAspectRatio="xMidYMid meet" — used to map client px ↔ viewBox units. */
function meetMetrics(rect: { width: number; height: number }) {
  const scale = Math.min(rect.width / W, rect.height / H);
  return {
    scale,
    offsetX: (rect.width - W * scale) / 2,
    offsetY: (rect.height - H * scale) / 2,
  };
}

/** Filter toolbar for the Location tab — region chips + the workloads present in the
 *  selected regions. Rendered by the inventory shell BELOW the tab bar. Toggling a region
 *  or workload drives the real inventory filters (locSel / wlSel), so the left facet menu,
 *  the grid, and every other tab narrow to the selection too. */
export function LocationFilterToolbar({
  resources,
  selectedLocations,
  onToggleLocation,
  onClearLocations,
  workloadName,
  selectedWorkloads,
  onToggleWorkload,
}: {
  resources: InventoryResource[];
  selectedLocations: Set<string>;
  onToggleLocation: (loc: string) => void;
  onClearLocations: () => void;
  workloadName: Record<string, string>;
  selectedWorkloads: Set<string>;
  onToggleWorkload: (id: string) => void;
}) {
  const workloads = useMemo(() => {
    const tally = new Map<string, number>(); // workload id (or __unassigned__) → count
    for (const r of resources) {
      if (!selectedLocations.has(r.location || "")) continue;
      const ws = r.workloads || [];
      if (ws.length === 0) tally.set("__unassigned__", (tally.get("__unassigned__") ?? 0) + 1);
      else for (const w of ws) tally.set(w.id, (tally.get(w.id) ?? 0) + 1);
    }
    return [...tally.entries()]
      .map(([id, count]) => ({ id, count, name: id === "__unassigned__" ? "Unassigned" : (workloadName[id] ?? id) }))
      .sort((a, b) => b.count - a.count);
  }, [resources, selectedLocations, workloadName]);

  if (selectedLocations.size === 0) return null;
  return (
    <div className="border-b border-brand/20 bg-brand/[0.03] px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Regions</span>
        {[...selectedLocations].map((loc) => (
          <span key={loc} className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-[12px] text-gray-700 shadow-sm ring-1 ring-gray-200">
            {friendlyLocation(loc)}
            <button onClick={() => onToggleLocation(loc)} className="text-gray-400 hover:text-red-500" title="Remove region">✕</button>
          </span>
        ))}
        <button onClick={onClearLocations} className="ml-1 rounded-md px-2 py-0.5 text-[11px] text-gray-500 hover:bg-white hover:text-gray-700">
          Clear regions
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-brand/10 pt-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Workloads here</span>
        {workloads.length === 0 && <span className="text-[11px] text-gray-400">No workloads in these regions.</span>}
        {workloads.map((w) => {
          const on = selectedWorkloads.has(w.id);
          return (
            <button
              key={w.id}
              onClick={() => onToggleWorkload(w.id)}
              title={on ? "Remove this workload from the filter" : "Filter the inventory to this workload"}
              className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[12px] transition ${
                on ? "bg-brand text-white shadow-sm" : "bg-white text-gray-700 ring-1 ring-gray-200 hover:ring-brand/40"
              }`}
            >
              <span className="max-w-[14rem] truncate">{w.name}</span>
              <span className={`tabular-nums ${on ? "text-white/80" : "text-gray-400"}`}>{w.count}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** "Location" inventory tab: a real world map plotting each Azure region by resource
 *  count. Supports zoom/pan and multi-region selection. Selection is CONTROLLED by the
 *  parent (drives the real `locSel` inventory filter, so the left menu + grid + other
 *  tabs all filter to the chosen regions). The plotted `resources` should be the
 *  inventory list with every filter EXCEPT location applied, so all regions stay
 *  visible (and selected ones are highlighted) while the rest of the UI is narrowed. */
export function LocationMode({
  resources,
  selectedLocations,
  onToggleLocation,
  onClear,
  resourceGroups,
  selectedRGs,
  onToggleRG,
  types,
  selectedTypes,
  onToggleType,
  subscriptions,
  selectedSubs,
  onToggleSub,
  subName,
}: {
  resources: InventoryResource[];
  selectedLocations: Set<string>;
  onToggleLocation: (loc: string) => void;
  onClear: () => void;
  resourceGroups: { key: string; count: number }[];
  selectedRGs: Set<string>;
  onToggleRG: (rg: string) => void;
  types: { key: string; count: number }[];
  selectedTypes: Set<string>;
  onToggleType: (t: string) => void;
  subscriptions: { key: string; count: number }[];
  selectedSubs: Set<string>;
  onToggleSub: (s: string) => void;
  subName: Record<string, string>;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const [dim, setDim] = useState<"region" | "rg" | "type" | "sub">("region");
  const [view, setView] = useState<View>({ scale: 1, tx: 0, ty: 0 });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null);

  const { points, unmapped, maxCount, totalMapped } = useMemo(() => {
    const byRegion = new Map<string, RegionPoint>();
    let unmappedCount = 0;
    for (const r of resources) {
      // Use the EXACT location value (matches the inventory facet keys + locSel), so a
      // selection here lines up with the location filter elsewhere.
      const key = r.location || "";
      const coordKey = key.toLowerCase().replace(/\s+/g, "");
      if (!coordKey || coordKey === "global") { if (coordKey === "global") unmappedCount++; continue; }
      const coord = REGION_COORDS[coordKey];
      if (!coord) { unmappedCount++; continue; }
      let p = byRegion.get(key);
      if (!p) {
        const { x, y } = project(coord[0], coord[1]);
        p = { key, name: friendlyLocation(key), count: 0, x, y, workloads: new Set() };
        byRegion.set(key, p);
      }
      p.count++;
      for (const w of r.workloads || []) p.workloads.add(w.name);
    }
    const pts = [...byRegion.values()].sort((a, b) => b.count - a.count);
    const max = pts.reduce((m, p) => Math.max(m, p.count), 0);
    const mapped = pts.reduce((s, p) => s + p.count, 0);
    return { points: pts, unmapped: unmappedCount, maxCount: max, totalMapped: mapped };
  }, [resources]);

  // Hub = busiest region; arcs flow from every other region to it.
  const hub = points[0];
  const radiusFor = (count: number) => 5 + 13 * Math.sqrt(count / (maxCount || 1));
  const colorFor = (count: number) => {
    const t = count / (maxCount || 1); // amber → red as intensity rises
    return t > 0.66 ? "#ef4444" : t > 0.33 ? "#f97316" : "#f59e0b";
  };
  const isSelected = (key: string) => selectedLocations.has(key);
  const isActive = (key: string) => hover === key || selectedLocations.has(key);
  const hasSel = selectedLocations.size > 0;

  // Project base coords through the current zoom/pan transform.
  const toScreen = (px: number, py: number) => ({ x: view.tx + view.scale * px, y: view.ty + view.scale * py });

  // --- zoom / pan ---------------------------------------------------------------------
  function zoomAt(factor: number, cx: number, cy: number) {
    setView((v) => {
      const ns = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, v.scale * factor));
      if (ns === v.scale) return v;
      const baseX = (cx - v.tx) / v.scale;
      const baseY = (cy - v.ty) / v.scale;
      return clampView(ns, cx - baseX * ns, cy - baseY * ns);
    });
  }
  function clientToViewBox(clientX: number, clientY: number) {
    const svg = svgRef.current;
    if (!svg) return { x: W / 2, y: H / 2 };
    const rect = svg.getBoundingClientRect();
    // The SVG element is taller than the viewBox aspect, so preserveAspectRatio="meet"
    // letterboxes the map vertically. Convert client px → viewBox units through the
    // actual rendered scale + centering offset, so wheel-zoom focuses where the cursor is.
    const m = meetMetrics(rect);
    return {
      x: (clientX - rect.left - m.offsetX) / m.scale,
      y: (clientY - rect.top - m.offsetY) / m.scale,
    };
  }
  function onWheel(e: React.WheelEvent<SVGSVGElement>) {
    e.preventDefault();
    const pt = clientToViewBox(e.clientX, e.clientY);
    zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, pt.x, pt.y);
  }
  function onPointerDown(e: React.PointerEvent<SVGSVGElement>) {
    if (view.scale <= 1) return;
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false };
    svgRef.current?.setPointerCapture(e.pointerId);
  }
  function onPointerMove(e: React.PointerEvent<SVGSVGElement>) {
    const d = drag.current;
    if (!d) return;
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const m = meetMetrics(rect);
    const dx = (e.clientX - d.x) / m.scale;
    const dy = (e.clientY - d.y) / m.scale;
    if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) d.moved = true;
    setView((v) => clampView(v.scale, d.tx + dx, d.ty + dy));
  }
  function onPointerUp(e: React.PointerEvent<SVGSVGElement>) {
    if (drag.current) svgRef.current?.releasePointerCapture(e.pointerId);
    drag.current = null;
  }

  const tooltipRegion = points.find((q) => isActive(q.key));

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl space-y-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <div className="text-sm text-gray-600">
            <b className="text-gray-800">{points.length}</b> region{points.length === 1 ? "" : "s"} ·{" "}
            <b className="text-gray-800">{totalMapped.toLocaleString()}</b> resources mapped
            {unmapped > 0 && <span className="text-gray-400"> · {unmapped} global/unmapped</span>}
            {hasSel && <span className="text-brand"> · {selectedLocations.size} region{selectedLocations.size === 1 ? "" : "s"} selected</span>}
          </div>
        </div>

        <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
          {/* World map */}
          <div className="relative min-w-0 flex-1 overflow-hidden rounded-xl border border-sky-200 bg-[#dbeaf5] shadow-sm">
            {/* Zoom controls */}
            <div className="absolute right-2 top-2 z-10 flex flex-col gap-1">
              <button
                onClick={() => zoomAt(1.6, W / 2, H / 2)}
                disabled={view.scale >= ZOOM_MAX}
                title="Zoom in"
                className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-300 bg-white/95 text-lg leading-none text-gray-700 shadow-sm hover:bg-white disabled:opacity-40"
              >
                +
              </button>
              <button
                onClick={() => zoomAt(1 / 1.6, W / 2, H / 2)}
                disabled={view.scale <= ZOOM_MIN}
                title="Zoom out"
                className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-300 bg-white/95 text-lg leading-none text-gray-700 shadow-sm hover:bg-white disabled:opacity-40"
              >
                −
              </button>
              <button
                onClick={() => setView({ scale: 1, tx: 0, ty: 0 })}
                disabled={view.scale === 1 && view.tx === 0 && view.ty === 0}
                title="Reset view"
                className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-300 bg-white/95 text-xs leading-none text-gray-700 shadow-sm hover:bg-white disabled:opacity-40"
              >
                ⟳
              </button>
            </div>

            <svg
              ref={svgRef}
              viewBox={`0 0 ${W} ${H}`}
              preserveAspectRatio="xMidYMid meet"
              className="block w-full"
              style={{ height: "clamp(420px, 78vh, 820px)", background: "#dbeaf5", touchAction: "none", cursor: view.scale > 1 ? "grab" : "default" }}
              onWheel={onWheel}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerLeave={onPointerUp}
            >
              <defs>
                <radialGradient id="loc-glow" cx="50%" cy="42%" r="80%">
                  <stop offset="0%" stopColor="#eef6fc" />
                  <stop offset="100%" stopColor="#dbeaf5" />
                </radialGradient>
              </defs>
              {/* Ocean */}
              <rect x={0} y={0} width={W} height={H} fill="url(#loc-glow)" />

              {/* Land — real country outlines (scaled/panned by the zoom transform) */}
              <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
                {COUNTRY_PATHS.map((d, i) => (
                  <path key={i} d={d} fill="#bfe0a8" stroke="#8fbf78" strokeWidth={0.4} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
                ))}
              </g>

              {/* Flow arcs from each region to the hub (drawn in screen space) */}
              {hub && points.map((p) => {
                if (p.key === hub.key) return null;
                const a = toScreen(p.x, p.y);
                const b = toScreen(hub.x, hub.y);
                const mx = (a.x + b.x) / 2;
                const my = (a.y + b.y) / 2 - Math.abs(a.x - b.x) * 0.28 - 18;
                const on = isActive(p.key) || isActive(hub.key);
                const dim = hasSel && !isSelected(p.key) && !isSelected(hub.key);
                return (
                  <path
                    key={`arc-${p.key}`}
                    d={`M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`}
                    fill="none"
                    stroke={on ? "#0369a1" : "#0ea5e9"}
                    strokeWidth={on ? 1.8 : 1.1}
                    strokeOpacity={dim ? 0.15 : on ? 0.95 : 0.5}
                    strokeDasharray="5 7"
                  >
                    <animate attributeName="stroke-dashoffset" from="48" to="0" dur="1.4s" repeatCount="indefinite" />
                  </path>
                );
              })}

              {/* Region markers (constant screen size regardless of zoom) */}
              {points.map((p) => {
                const s = toScreen(p.x, p.y);
                if (s.x < -40 || s.x > W + 40 || s.y < -40 || s.y > H + 40) return null;
                const r = radiusFor(p.count);
                const c = colorFor(p.count);
                const on = isActive(p.key);
                const sel = isSelected(p.key);
                const dim = hasSel && !sel;
                return (
                  <g
                    key={p.key}
                    onMouseEnter={() => setHover(p.key)}
                    onMouseLeave={() => setHover(null)}
                    onClick={() => { if (!drag.current?.moved) onToggleLocation(p.key); }}
                    style={{ cursor: "pointer", opacity: dim ? 0.4 : 1 }}
                  >
                    {!dim && (
                      <circle cx={s.x} cy={s.y} r={r} fill={c} opacity={0.18}>
                        <animate attributeName="r" from={r} to={r * 2.1} dur="2.2s" repeatCount="indefinite" />
                        <animate attributeName="opacity" from="0.35" to="0" dur="2.2s" repeatCount="indefinite" />
                      </circle>
                    )}
                    {sel && <circle cx={s.x} cy={s.y} r={r + 3.5} fill="none" stroke="#0369a1" strokeWidth={2} />}
                    <circle cx={s.x} cy={s.y} r={r} fill={c} opacity={on ? 0.98 : 0.9} stroke="#ffffff" strokeWidth={1.5} />
                    <text x={s.x} y={s.y + 3.2} textAnchor="middle" fontSize={Math.max(8, r * 0.8)} fontWeight={700} fill="#ffffff" style={{ pointerEvents: "none" }}>
                      {p.count}
                    </text>
                  </g>
                );
              })}

              {/* Active-region tooltip — drawn LAST so no marker can paint over it. */}
              {tooltipRegion && (() => {
                const p = tooltipRegion;
                const s = toScreen(p.x, p.y);
                const detail = `${p.count} resources · ${p.workloads.size} workload${p.workloads.size === 1 ? "" : "s"}`;
                const tw = Math.max(96, p.name.length * 7.2, detail.length * 6.2) + 16;
                const th = 34;
                const r = radiusFor(p.count);
                let bx = s.x + r + 8;
                if (bx + tw > W) bx = s.x - r - 8 - tw;
                let by = s.y - th - 6;
                if (by < 2) by = s.y + r + 8;
                return (
                  <g style={{ pointerEvents: "none" }}>
                    <rect x={bx} y={by} width={tw} height={th} rx={5} fill="#ffffff" stroke="#cbd5e1" opacity={0.98} />
                    <text x={bx + 8} y={by + 13} fontSize={11} fontWeight={700} fill="#1e293b">{p.name}</text>
                    <text x={bx + 8} y={by + 26} fontSize={10} fill="#64748b">{detail}</text>
                  </g>
                );
              })()}

              {points.length === 0 && (
                <text x={W / 2} y={H / 2} textAnchor="middle" fontSize={16} fill="#475569">
                  No mappable regions in the current selection.
                </text>
              )}
            </svg>
          </div>

          {/* Breakdown panel: Region (drives the map) / Resource group / Type. Every
              dimension here filters the whole inventory (locSel / rgSel / typeSel). */}
          <div className="w-full shrink-0 lg:w-64">
            <div className="rounded-xl border border-gray-200 bg-white p-3 shadow-sm">
              {/* Dimension switcher */}
              <div className="mb-2 flex gap-0.5 rounded-lg bg-gray-100 p-0.5">
                {([
                  ["region", "Region"],
                  ["rg", "Group"],
                  ["type", "Type"],
                  ["sub", "Sub"],
                ] as const).map(([id, label]) => {
                  const n = id === "region" ? selectedLocations.size : id === "rg" ? selectedRGs.size : id === "type" ? selectedTypes.size : selectedSubs.size;
                  return (
                    <button
                      key={id}
                      onClick={() => setDim(id)}
                      className={`flex-1 rounded-md px-1 py-1 text-[11px] font-medium transition ${dim === id ? "bg-white text-brand shadow-sm" : "text-gray-500 hover:text-gray-700"}`}
                    >
                      {label}{n > 0 && <span className="ml-0.5 rounded-full bg-brand/10 px-1 text-[9px] text-brand">{n}</span>}
                    </button>
                  );
                })}
              </div>

              {dim === "region" && (
                <>
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">By region</span>
                    {hasSel && <button onClick={onClear} className="text-[10px] text-brand hover:underline">Clear</button>}
                  </div>
                  {points.length === 0 ? (
                    <p className="text-xs text-gray-400">No resources to map.</p>
                  ) : (
                    <div className="space-y-0.5">
                      {points.map((p) => {
                        const sel = isSelected(p.key);
                        return (
                          <button
                            key={p.key}
                            onMouseEnter={() => setHover(p.key)}
                            onMouseLeave={() => setHover(null)}
                            onClick={() => onToggleLocation(p.key)}
                            className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition ${sel ? "bg-brand/10 ring-1 ring-brand/30" : isActive(p.key) ? "bg-brand/5" : "hover:bg-gray-50"}`}
                          >
                            <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border" style={{ borderColor: sel ? "#0369a1" : "#cbd5e1", background: sel ? "#0369a1" : "transparent" }}>
                              {sel && <span className="text-[9px] leading-none text-white">✓</span>}
                            </span>
                            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: colorFor(p.count) }} />
                            <span className="min-w-0 flex-1 truncate text-[13px] text-gray-700" title={p.name}>{p.name}</span>
                            <span className="shrink-0 text-[12px] font-medium tabular-nums text-gray-500">{p.count}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  <div className="mt-2 border-t pt-2 text-[10px] text-gray-400">
                    Click a region to filter the whole inventory to it (multi-select). Scroll
                    or use +/− to zoom, drag to pan.
                  </div>
                </>
              )}

              {dim === "rg" && (
                <BreakdownList
                  title="By resource group"
                  rows={resourceGroups}
                  selected={selectedRGs}
                  onToggle={onToggleRG}
                  label={(k) => k}
                  empty="No resource groups in view."
                  hint="Click a resource group to filter the whole inventory to it (multi-select)."
                />
              )}

              {dim === "type" && (
                <BreakdownList
                  title="By resource type"
                  rows={types}
                  selected={selectedTypes}
                  onToggle={onToggleType}
                  label={(k) => friendlyResourceType(k)}
                  empty="No resource types in view."
                  hint="Click a type to filter the whole inventory to it (multi-select)."
                />
              )}

              {dim === "sub" && (
                <BreakdownList
                  title="By subscription"
                  rows={subscriptions}
                  selected={selectedSubs}
                  onToggle={onToggleSub}
                  label={(k) => subName[k] || k}
                  empty="No subscriptions in view."
                  hint="Click a subscription to filter the whole inventory to it (multi-select)."
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** A simple selectable count leaderboard for the Resource group / Type breakdowns,
 *  driving the matching inventory filter (rgSel / typeSel). */
function BreakdownList({
  title,
  rows,
  selected,
  onToggle,
  label,
  empty,
  hint,
}: {
  title: string;
  rows: { key: string; count: number }[];
  selected: Set<string>;
  onToggle: (key: string) => void;
  label: (key: string) => string;
  empty: string;
  hint: string;
}) {
  const visible = rows.filter((r) => r.count > 0 || selected.has(r.key)).sort((a, b) => b.count - a.count);
  const max = visible.reduce((m, r) => Math.max(m, r.count), 0) || 1;
  return (
    <>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">{title}</span>
        {selected.size > 0 && (
          <button onClick={() => [...selected].forEach(onToggle)} className="text-[10px] text-brand hover:underline">Clear</button>
        )}
      </div>
      {visible.length === 0 ? (
        <p className="text-xs text-gray-400">{empty}</p>
      ) : (
        <div className="max-h-[60vh] space-y-0.5 overflow-y-auto">
          {visible.map((r) => {
            const sel = selected.has(r.key);
            return (
              <button
                key={r.key}
                onClick={() => onToggle(r.key)}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition ${sel ? "bg-brand/10 ring-1 ring-brand/30" : "hover:bg-gray-50"}`}
              >
                <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border" style={{ borderColor: sel ? "#0369a1" : "#cbd5e1", background: sel ? "#0369a1" : "transparent" }}>
                  {sel && <span className="text-[9px] leading-none text-white">✓</span>}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] text-gray-700" title={label(r.key)}>{label(r.key)}</span>
                  <span className="mt-0.5 block h-1 w-full overflow-hidden rounded-full bg-gray-100">
                    <span className="block h-full bg-brand/50" style={{ width: `${Math.max(4, (r.count / max) * 100)}%` }} />
                  </span>
                </span>
                <span className="shrink-0 text-[12px] font-medium tabular-nums text-gray-500">{r.count}</span>
              </button>
            );
          })}
        </div>
      )}
      <div className="mt-2 border-t pt-2 text-[10px] text-gray-400">{hint}</div>
    </>
  );
}
