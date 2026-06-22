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
            {q.data.concentration_risk.slice(0, 8).map((c) => (
              <button key={c.id} onClick={() => onFocus(c.id)} className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-1 text-left hover:bg-slate-50">
                <span className="truncate">{KIND_META[c.kind as keyof typeof KIND_META]?.glyph} {c.label}</span>
                <span className="shrink-0 text-[11px] text-slate-400">bc {c.betweenness} · deg {c.degree}</span>
              </button>
            ))}
          </Section>
          <Section title={`Communities (${q.data.community_count})`}>
            {q.data.communities.slice(0, 6).map((c, i) => (
              <div key={i} className="rounded px-1.5 py-1">
                <div className="font-medium text-slate-700">Cluster {i + 1} · {c.size} nodes</div>
                <div className="truncate text-[11px] text-slate-400">{c.sample.join(", ")}</div>
              </div>
            ))}
          </Section>
          <Section title="Orphans">
            <Row label="Unowned resources" value={q.data.orphans.unowned_count} />
            <Row label="Workloads w/o architecture" value={q.data.orphans.workloads_without_architecture.length} />
            <Row label="Architectures w/o workload" value={q.data.orphans.architectures_without_workload.length} />
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

// ----------------------------------------------------------------- minimap
export function Minimap({ cy }: { cy: any }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
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
      nodes.forEach((n: any) => {
        if (n.style("display") === "none") return;
        const p = n.position();
        ctx.fillStyle = n.style("background-color") || "#94a3b8";
        ctx.beginPath();
        ctx.arc(p.x * scale + ox, p.y * scale + oy, 1.6, 0, Math.PI * 2);
        ctx.fill();
      });
      // viewport rectangle
      const ext = cy.extent();
      ctx.strokeStyle = "#1e293b";
      ctx.lineWidth = 1;
      ctx.strokeRect(ext.x1 * scale + ox, ext.y1 * scale + oy, ext.w * scale, ext.h * scale);
    };
    const onChange = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); };
    cy.on("render position pan zoom add remove", onChange);
    draw();
    return () => { cy.off("render position pan zoom add remove", onChange); cancelAnimationFrame(raf); };
  }, [cy]);
  return <canvas ref={ref} width={180} height={120} className="rounded border border-slate-200 bg-white/90 shadow-sm" />;
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

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className="text-slate-700">{value}</span>
    </div>
  );
}
