import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type GraphAnalytics,
  type GraphNarrative,
  type GraphNode,
  type GraphView,
} from "../../api";
import { formatError } from "../../utils/format";
import { KIND_META } from "./graphStyle";

// ----------------------------------------------------------------- analytics panel
export function AnalyticsPanel({ connectionId, onFocus, onClose }: { connectionId: string; onFocus: (id: string) => void; onClose: () => void }) {
  const q = useQuery<GraphAnalytics>({
    queryKey: ["graph-analytics", connectionId],
    queryFn: () => api.graphAnalytics(connectionId),
    staleTime: 60_000,
  });
  return (
    <SidePanel title="Estate analytics" onClose={onClose}>
      {q.isLoading && <div className="text-slate-500">Computing centrality, communities, orphans…</div>}
      {q.isError && <div className="text-red-600">{formatError(q.error)}</div>}
      {q.data && (
        <div className="space-y-4">
          <Section title="Concentration risk (load-bearing)">
            {(() => {
              const rows = q.data.concentration_risk.slice(0, 8);
              const max = Math.max(1, ...rows.map((r) => r.betweenness));
              return rows.map((c) => {
                const pct = Math.round((c.betweenness / max) * 100);
                return (
                  <button key={c.id} onClick={() => onFocus(c.id)} className="block w-full rounded px-1.5 py-1 text-left hover:bg-slate-50">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate">{KIND_META[c.kind as keyof typeof KIND_META]?.glyph} {c.label}</span>
                      <span className="shrink-0 text-[10px] text-slate-400">{loadLabel(pct)} · {c.degree} links</span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                      <div className="h-full rounded-full" style={{ width: `${Math.max(4, pct)}%`, backgroundColor: pct > 66 ? "#dc2626" : pct > 33 ? "#d97706" : "#0ea5e9" }} />
                    </div>
                  </button>
                );
              });
            })()}
          </Section>
          <Section title="Orphans & hygiene">
            <div className="flex items-center gap-3 px-1 py-1">
              <Donut
                segments={[
                  { value: q.data.orphans.unowned_count, color: "#f59e0b" },
                  { value: Math.max(0, (q.data.stats.by_kind?.resource || 0) - q.data.orphans.unowned_count), color: "#0ea5e9" },
                ]}
                center={`${q.data.orphans.unowned_count}`}
                sub="unowned"
              />
              <div className="flex-1 space-y-1 text-[11px]">
                <LegendDot color="#f59e0b" label={`${q.data.orphans.unowned_count} unowned resources`} />
                <LegendDot color="#0ea5e9" label={`${q.data.stats.by_kind?.resource || 0} resources total`} />
                <LegendDot color="#94a3b8" label={`${q.data.orphans.workloads_without_architecture.length} workloads w/o architecture`} />
                <LegendDot color="#cbd5e1" label={`${q.data.orphans.architectures_without_workload.length} architectures w/o workload`} />
              </div>
            </div>
          </Section>
          <Section title={`Communities (${q.data.community_count})`}>
            {q.data.communities.slice(0, 6).map((c, i) => (
              <div key={i} className="rounded px-1.5 py-1">
                <div className="flex items-center gap-2">
                  <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: COMMUNITY_COLORS[i % COMMUNITY_COLORS.length] }} />
                  <span className="font-medium text-slate-700">Cluster {i + 1}</span>
                  <span className="ml-auto text-[10px] text-slate-400">{c.size} nodes</span>
                </div>
                <div className="truncate pl-4 text-[11px] text-slate-400">{c.sample.join(", ")}</div>
              </div>
            ))}
          </Section>
          <Section title={`Candidate workloads (${q.data.candidate_workloads.length})`}>
            {q.data.candidate_workloads.length === 0 && <div className="text-[11px] text-slate-400">No unowned clusters of significance.</div>}
            {q.data.candidate_workloads.slice(0, 5).map((c, i) => (
              <div key={i} className="rounded px-1.5 py-1">
                <div className="font-medium text-slate-700">{c.size} resources · {c.reason}</div>
                <div className="truncate text-[11px] text-slate-400">{c.types.map((t) => `${t.type.split("/").pop()} ×${t.count}`).join(", ")}</div>
              </div>
            ))}
          </Section>
        </div>
      )}
    </SidePanel>
  );
}

const COMMUNITY_COLORS = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6"];

function loadLabel(pct: number): string {
  if (pct > 66) return "high load";
  if (pct > 33) return "medium";
  return "low";
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return <div className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: color }} /><span className="text-slate-600">{label}</span></div>;
}

function Donut({ segments, center, sub }: { segments: { value: number; color: string }[]; center: string; sub: string }) {
  const total = Math.max(1, segments.reduce((s, x) => s + Math.max(0, x.value), 0));
  const r = 22, c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width="64" height="64" viewBox="0 0 64 64" className="shrink-0">
      <circle cx="32" cy="32" r={r} fill="none" stroke="#f1f5f9" strokeWidth="9" />
      {segments.map((s, i) => {
        const frac = Math.max(0, s.value) / total;
        const dash = frac * c;
        const el = (
          <circle key={i} cx="32" cy="32" r={r} fill="none" stroke={s.color} strokeWidth="9"
            strokeDasharray={`${dash} ${c - dash}`} strokeDashoffset={-offset} transform="rotate(-90 32 32)" />
        );
        offset += dash;
        return el;
      })}
      <text x="32" y="30" textAnchor="middle" fontSize="13" fontWeight="700" fill="#0f172a">{center}</text>
      <text x="32" y="42" textAnchor="middle" fontSize="7" fill="#94a3b8">{sub}</text>
    </svg>
  );
}

// ----------------------------------------------------------------- ask + narrative panel
export function AskPanel({ connectionId, scopeKind, scopeId, onMatched, onClose }: {
  connectionId: string;
  scopeKind: string;
  scopeId: string;
  onMatched: (nodes: GraphNode[]) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<{ count: number; explanation: string; used_ai: boolean } | null>(null);
  const [narr, setNarr] = useState<GraphNarrative | null>(null);
  const [narrBusy, setNarrBusy] = useState(false);
  const [err, setErr] = useState("");

  const ask = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setErr("");
    try {
      const res = await api.graphAsk(q, connectionId);
      setAnswer({ count: res.count, explanation: res.explanation, used_ai: res.used_ai });
      onMatched(res.nodes);
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy(false);
    }
  };

  const narrate = async () => {
    setNarrBusy(true);
    setErr("");
    try {
      setNarr(await api.graphNarrative(scopeKind, scopeId, connectionId));
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setNarrBusy(false);
    }
  };

  const EXAMPLES = [
    "internet-facing workloads without backup",
    "workloads with critical findings",
    "shared services used by multiple workloads",
    "unowned resources",
  ];

  return (
    <SidePanel title="Ask the graph" onClose={onClose}>
      <div className="space-y-3">
        <div className="flex gap-1.5">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void ask()}
            placeholder="e.g. internet-facing workloads without backup"
            className="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
          <button onClick={() => void ask()} disabled={busy} className="rounded-md bg-slate-800 px-3 py-1.5 text-xs text-white disabled:opacity-50">
            {busy ? "…" : "Ask"}
          </button>
        </div>
        <div className="flex flex-wrap gap-1">
          {EXAMPLES.map((ex) => (
            <button key={ex} onClick={() => { setQ(ex); }} className="rounded-full border border-slate-200 px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-50">{ex}</button>
          ))}
        </div>
        {answer && (
          <div className="rounded-md border border-blue-200 bg-blue-50 p-2 text-xs text-blue-700">
            Matched {answer.count} node(s). {answer.explanation} {answer.used_ai ? "(AI)" : ""}
          </div>
        )}
        {err && <div className="text-xs text-red-600">{err}</div>}

        <div className="border-t pt-3">
          <button onClick={() => void narrate()} disabled={narrBusy} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs hover:bg-slate-50 disabled:opacity-50">
            {narrBusy ? "Writing…" : "✨ AI narrative"}
          </button>
          {narr && (
            <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-slate-600">{narr.narrative}</p>
          )}
        </div>
      </div>
    </SidePanel>
  );
}

// ----------------------------------------------------------------- saved views panel
export function ViewsPanel({ onApply, onSaveCurrent, onClose }: {
  onApply: (v: GraphView) => void;
  onSaveCurrent: (name: string) => Promise<void>;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const q = useQuery({ queryKey: ["graph-views"], queryFn: api.graphViews, staleTime: 10_000 });
  const save = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      await onSaveCurrent(name.trim());
      setName("");
      await q.refetch();
    } finally {
      setSaving(false);
    }
  };
  const del = async (id: string) => {
    await api.graphDeleteView(id);
    await q.refetch();
  };
  return (
    <SidePanel title="Saved views" onClose={onClose}>
      <div className="mb-3 flex gap-1.5">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name this view" className="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        <button onClick={() => void save()} disabled={saving} className="rounded-md bg-slate-800 px-3 py-1.5 text-xs text-white disabled:opacity-50">Save</button>
      </div>
      <div className="space-y-1">
        {(q.data?.views || []).length === 0 && <div className="text-[11px] text-slate-400">No saved views yet.</div>}
        {(q.data?.views || []).map((v) => (
          <div key={v.id} className="flex items-center justify-between gap-2 rounded px-1.5 py-1 hover:bg-slate-50">
            <button onClick={() => onApply(v)} className="flex-1 truncate text-left text-slate-700">{v.name || "Untitled"}</button>
            <span className="shrink-0 text-[10px] text-slate-400">{v.lens !== "none" ? v.lens : v.scope_kind}</span>
            <button onClick={() => void del(v.id)} className="shrink-0 text-slate-300 hover:text-red-500" title="Delete">✕</button>
          </div>
        ))}
      </div>
    </SidePanel>
  );
}

// ----------------------------------------------------------------- zoom control
// A vertical zoom slider + / − buttons + a "fit" button, anchored over the canvas. Zooms
// about the viewport centre so the framing stays stable. Subscribes to cytoscape's own zoom
// event so the slider stays in sync with wheel/pinch zoom too.
export function ZoomControl({ cy, dark }: { cy: any; dark: boolean }) {
  const [zoom, setZoom] = useState(1);
  const minRef = useRef(0.05);
  const maxRef = useRef(4);

  useEffect(() => {
    if (!cy) return;
    minRef.current = cy.minZoom() || 0.05;
    maxRef.current = cy.maxZoom() || 4;
    const sync = () => setZoom(cy.zoom());
    sync();
    cy.on("zoom", sync);
    return () => cy.off("zoom", sync);
  }, [cy]);

  // Slider position is 0..1 on a LOG scale (zoom spans orders of magnitude).
  const lmin = Math.log(minRef.current);
  const lmax = Math.log(maxRef.current);
  const pos = lmax > lmin ? (Math.log(Math.max(minRef.current, Math.min(maxRef.current, zoom))) - lmin) / (lmax - lmin) : 0.5;

  const zoomToCentre = (z: number) => {
    if (!cy) return;
    const clamped = Math.max(minRef.current, Math.min(maxRef.current, z));
    cy.zoom({ level: clamped, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };
  const fromPos = (p: number) => Math.exp(lmin + p * (lmax - lmin));
  const step = (factor: number) => zoomToCentre((cy ? cy.zoom() : 1) * factor);

  const btn = dark
    ? "border-slate-700 bg-slate-800/90 text-slate-200 hover:bg-slate-700"
    : "border-slate-200 bg-white/90 text-slate-600 hover:bg-slate-50";

  return (
    <div className={`flex flex-col items-center gap-1 rounded-lg border px-1 py-1.5 shadow-sm ${dark ? "border-slate-700 bg-slate-800/80" : "border-slate-200 bg-white/85"}`}>
      <button onClick={() => step(1.25)} className={`flex h-6 w-6 items-center justify-center rounded border text-sm ${btn}`} title="Zoom in (+)">+</button>
      <input
        type="range"
        min={0}
        max={1000}
        value={Math.round(pos * 1000)}
        onChange={(e) => zoomToCentre(fromPos(Number(e.target.value) / 1000))}
        className="graph-zoom-slider"
        style={{ writingMode: "vertical-lr" as React.CSSProperties["writingMode"], direction: "rtl", height: 96, width: 18 }}
        title={`Zoom ${Math.round(zoom * 100)}%`}
        aria-label="Zoom level"
      />
      <button onClick={() => step(0.8)} className={`flex h-6 w-6 items-center justify-center rounded border text-sm ${btn}`} title="Zoom out (−)">−</button>
      <button onClick={() => { if (cy) cy.fit(undefined, 50); }} className={`mt-0.5 flex h-6 w-6 items-center justify-center rounded border text-[11px] ${btn}`} title="Fit to screen">⤢</button>
      <div className={`text-[9px] tabular-nums ${dark ? "text-slate-400" : "text-slate-400"}`}>{Math.round(zoom * 100)}%</div>
    </div>
  );
}

// ----------------------------------------------------------------- minimap
export function Minimap({ cy }: { cy: any }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const xform = useRef<{ scale: number; ox: number; oy: number }>({ scale: 1, ox: 0, oy: 0 });
  useEffect(() => {
    if (!cy) return;
    let raf = 0;
    const draw = () => {
      const canvas = ref.current;
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H);
      const nodes = cy.nodes();
      if (nodes.length === 0) return;
      const bb = cy.elements().boundingBox();
      const sx = (bb.w || 1), sy = (bb.h || 1);
      const scale = Math.min(W / sx, H / sy) * 0.9;
      const ox = (W - sx * scale) / 2 - bb.x1 * scale;
      const oy = (H - sy * scale) / 2 - bb.y1 * scale;
      xform.current = { scale, ox, oy };
      nodes.forEach((n: any) => {
        if (n.style("display") === "none") return;
        const p = n.position();
        ctx.fillStyle = n.data("ring") || n.style("border-color") || "#94a3b8";
        ctx.beginPath();
        ctx.arc(p.x * scale + ox, p.y * scale + oy, 1.6, 0, Math.PI * 2);
        ctx.fill();
      });
      // viewport rectangle
      const ext = cy.extent();
      ctx.fillStyle = "rgba(59,130,246,0.12)";
      ctx.fillRect(ext.x1 * scale + ox, ext.y1 * scale + oy, ext.w * scale, ext.h * scale);
      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = 1.2;
      ctx.strokeRect(ext.x1 * scale + ox, ext.y1 * scale + oy, ext.w * scale, ext.h * scale);
    };
    const onChange = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); };
    cy.on("render position pan zoom add remove", onChange);
    draw();
    return () => { cy.off("render position pan zoom add remove", onChange); cancelAnimationFrame(raf); };
  }, [cy]);

  // Click-to-jump: convert the canvas click back to graph coords and recenter.
  const onClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!cy) return;
    const canvas = ref.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const cx = ((e.clientX - rect.left) / rect.width) * canvas.width;
    const cy2 = ((e.clientY - rect.top) / rect.height) * canvas.height;
    const { scale, ox, oy } = xform.current;
    if (!scale) return;
    const gx = (cx - ox) / scale;
    const gy = (cy2 - oy) / scale;
    // Pan so the clicked graph point lands at the viewport centre.
    const z = cy.zoom();
    const vw = cy.width(), vh = cy.height();
    cy.animate({ pan: { x: vw / 2 - gx * z, y: vh / 2 - gy * z } }, { duration: 250 });
  };

  return <canvas ref={ref} onClick={onClick} width={180} height={120} className="cursor-pointer rounded border border-slate-200 bg-white/90 shadow-sm" title="Click to jump" />;
}

// ----------------------------------------------------------------- shared bits
function SidePanel({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="absolute left-0 top-0 z-30 flex h-full w-72 flex-col border-r bg-white shadow-xl">
      <div className="flex items-center justify-between border-b px-3 py-2.5">
        <span className="text-sm font-semibold text-slate-800">{title}</span>
        <button onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100">✕</button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3 text-sm">{children}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}
