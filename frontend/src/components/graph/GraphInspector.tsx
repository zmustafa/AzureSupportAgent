import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import type { GraphNode, GraphNodeDetail, GraphNodeKind } from "../../api";
import { KIND_META } from "./graphStyle";

function Row({ label, value }: { label: string; value: ReactNode }) {
  if (value === undefined || value === null || value === "") return null;
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="shrink-0 text-slate-400">{label}</span>
      <span className="truncate text-right text-slate-700">{value}</span>
    </div>
  );
}

const CTA: { key: string; label: string }[] = [
  { key: "inventory", label: "Inventory" },
  { key: "architecture", label: "Architecture" },
  { key: "memory", label: "Memory" },
  { key: "assessment", label: "Assessment" },
  { key: "change", label: "Change Explorer" },
  { key: "rbac", label: "RBAC" },
  { key: "telemetry", label: "Telemetry Intel" },
  { key: "backupdr", label: "Backup & DR" },
];

export function GraphInspector({
  detail,
  loading,
  kind,
  onClose,
  onExpand,
  onBlastRadius,
  onPathFrom,
  onPathTo,
  onDrift,
  onWarRoom,
}: {
  detail: GraphNodeDetail | undefined;
  loading: boolean;
  kind: GraphNodeKind | "";
  onClose: () => void;
  onExpand: () => void;
  onBlastRadius: () => void;
  onPathFrom: () => void;
  onPathTo: () => void;
  onDrift: () => void;
  onWarRoom: () => void;
}) {
  const navigate = useNavigate();
  const node = detail?.node;
  const dossier = detail?.dossier;
  const meta = kind ? KIND_META[kind] : undefined;
  const links = (dossier?.links || {}) as Record<string, string>;
  const isWorkload = kind === "workload";

  // Carry the workload context to whatever section the user opens, so the destination loads
  // already scoped to this workload instead of the last-used/empty scope. Each target reads it
  // via the mechanism it supports: `?workload_id=` (coverage/change/backupdr/perf/tag/radar +
  // the inventory/rbac readers we added), a sessionStorage handoff (Telemetry Intel,
  // Assessments), or a backend-provided scoped URL (Architecture / Memory / the specific run).
  const openIn = (key: string) => {
    const wid: string = node?.data?.workload_id || (node?.id?.startsWith("wl:") ? node.id.slice(3) : "");
    const wname: string = node?.label || "";
    const archId: string = Array.isArray(dossier?.architectures) && dossier!.architectures[0]?.id ? dossier!.architectures[0].id : "";
    const runId: string = dossier?.risk?.run_id || "";
    const wlParam = wid ? `?workload_id=${encodeURIComponent(wid)}&workload_name=${encodeURIComponent(wname)}` : "";
    switch (key) {
      case "inventory":
        navigate(`/inventory${wlParam}`);
        break;
      case "architecture":
        navigate(archId ? `/architectures/${archId}` : links.architecture || "/architectures");
        break;
      case "memory":
        if (archId) navigate(`/architectures/${archId}/memory`);
        break;
      case "assessment":
        if (runId) navigate(`/assessments/${runId}`);
        else { try { if (wid) sessionStorage.setItem("azsup.assessWorkload", wid); } catch { /* ignore */ } navigate("/assessments"); }
        break;
      case "change":
        navigate(`/change-explorer${wlParam}`);
        break;
      case "rbac":
        navigate(`/rbac/effective${wlParam}`);
        break;
      case "telemetry":
        try { if (wid) sessionStorage.setItem("azsup.teleintelHandoff", JSON.stringify({ workloadId: wid })); } catch { /* ignore */ }
        navigate("/telemetry-intel");
        break;
      case "backupdr":
        navigate(`/backupdr${wlParam}`);
        break;
    }
  };

  // Hide CTAs that have no destination for this workload (e.g. no architecture → no Memory).
  const hasArch = Array.isArray(dossier?.architectures) && dossier!.architectures.length > 0;
  const ctaVisible = (key: string) => (key === "architecture" || key === "memory" ? hasArch : true);

  return (
    <div className="absolute right-0 top-0 z-30 flex h-full w-80 flex-col border-l bg-white shadow-xl">
      <div className="flex items-start justify-between border-b px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-slate-400">
            <span>{meta?.glyph}</span>
            {meta?.label || "Node"}
          </div>
          <div className="truncate text-sm font-semibold text-slate-800">{node?.label || "…"}</div>
        </div>
        <button onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100" title="Close">✕</button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-sm">
        {loading && <div className="text-slate-500">Loading…</div>}
        {!loading && detail && detail.found === false && <div className="text-slate-500">{detail.detail || "No details available."}</div>}
        {!loading && node && dossier && <DossierBody node={node} dossier={dossier} />}

        {/* Workload CTA grid (Phase 2) — each opens the section scoped to this workload */}
        {isWorkload && dossier && (
          <div className="mt-4">
            <div className="mb-1.5 text-[11px] font-semibold uppercase text-slate-400">Open in</div>
            <div className="grid grid-cols-2 gap-1.5">
              {CTA.filter((c) => ctaVisible(c.key)).map((c) => (
                <button
                  key={c.key}
                  onClick={() => openIn(c.key)}
                  className="rounded-md border border-slate-200 px-2 py-1 text-left text-xs text-slate-600 hover:bg-slate-50"
                >
                  {c.label} →
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Action bar */}
      <div className="flex flex-wrap gap-1.5 border-t px-4 py-3">
        {node?.expandable && (
          <button onClick={onExpand} className="rounded-md bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-700">Expand</button>
        )}
        <button onClick={onBlastRadius} className="rounded-md border border-red-200 px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50" title="Impact set from this node">Blast radius</button>
        <button onClick={onPathFrom} className="rounded-md border border-blue-200 px-2.5 py-1.5 text-xs text-blue-600 hover:bg-blue-50">Path: from</button>
        <button onClick={onPathTo} className="rounded-md border border-blue-200 px-2.5 py-1.5 text-xs text-blue-600 hover:bg-blue-50">Path: to</button>
        {isWorkload && <button onClick={onDrift} className="rounded-md border border-violet-200 px-2.5 py-1.5 text-xs text-violet-600 hover:bg-violet-50">Drift</button>}
        {isWorkload && <button onClick={onWarRoom} className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-50">War Room</button>}
      </div>
    </div>
  );
}

function DossierBody({ node, dossier }: { node: GraphNode; dossier: Record<string, any> }) {
  const k = dossier.kind as string;
  if (k === "workload") {
    const risk = dossier.risk;
    return (
      <div className="space-y-3">
        {dossier.description && <p className="text-slate-600">{dossier.description}</p>}
        <div className="flex flex-wrap gap-1.5">
          {node.data.criticality && <Chip tone={critTone(node.data.criticality)}>{node.data.criticality}</Chip>}
          {node.data.environment && <Chip tone="slate">{node.data.environment}</Chip>}
          {node.data.workload_type && <Chip tone="slate">{String(node.data.workload_type).replace(/_/g, " ")}</Chip>}
          <Chip tone="slate">{dossier.member_resources ?? 0} resources</Chip>
        </div>
        {risk && (
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-2.5">
            <div className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Latest assessment</div>
            <div className="flex items-center gap-3">
              <ScoreRing score={risk.score} />
              <div className="flex-1 space-y-1.5">
                <div className="flex flex-wrap gap-1.5">
                  {risk.failed != null && <Chip tone={risk.failed > 0 ? "red" : "green"}>{risk.failed} failing</Chip>}
                  {risk.passed != null && <Chip tone="green">{risk.passed} passing</Chip>}
                  {risk.severity && <Chip tone={sevTone(risk.severity)}>{risk.severity}</Chip>}
                </div>
                <RiskBar passed={risk.passed} failed={risk.failed} na={risk.na} />
              </div>
            </div>
          </div>
        )}
        {Array.isArray(dossier.architectures) && dossier.architectures.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Architectures</div>
            {dossier.architectures.map((a: any) => (
              <div key={a.id} className="truncate text-slate-700">📐 {a.name}</div>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (k === "resource") {
    return (
      <div className="space-y-0.5">
        <Row label="Type" value={dossier.type} />
        <Row label="Resource group" value={dossier.resource_group} />
        <Row label="Location" value={dossier.location} />
        <Row label="SKU" value={dossier.sku || dossier.tier} />
        <Row label="Subscription" value={dossier.subscription_id} />
        {node.data?.drift && <Row label="Drift" value={driftLabel(node.data.drift)} />}
        {Array.isArray(dossier.flags) && dossier.flags.length > 0 && <Row label="Flags" value={dossier.flags.join(", ")} />}
        {Array.isArray(dossier.workloads) && dossier.workloads.length > 0 && <Row label="Workload" value={dossier.workloads.map((w: any) => w.name).join(", ")} />}
      </div>
    );
  }
  if (k === "architecture") {
    return (
      <div className="space-y-0.5">
        {dossier.description && <p className="pb-1 text-slate-600">{dossier.description}</p>}
        <Row label="Workload" value={dossier.workload_name} />
        <Row label="State" value={dossier.state} />
        <Row label="Source" value={dossier.source} />
        <Row label="Nodes" value={dossier.node_count} />
        <Row label="Edges" value={dossier.edge_count} />
      </div>
    );
  }
  if (k === "subscription") {
    return (
      <div className="space-y-0.5">
        <Row label="Subscription" value={dossier.subscription_id} />
        <Row label="Resources" value={dossier.resource_count} />
        <Row label="Resource groups" value={dossier.resource_group_count} />
      </div>
    );
  }
  if (k === "assessment_finding") {
    return (
      <div className="space-y-2">
        <div className="space-y-0.5">
          <Row label="Pillar" value={dossier.pillar} />
          <Row label="Severity" value={dossier.severity} />
          <Row label="Status" value={dossier.status} />
        </div>
        {dossier.rationale && <p className="text-slate-600">{dossier.rationale}</p>}
        {dossier.remediation && <div className="rounded-md border border-slate-200 bg-slate-50 p-2 text-xs text-slate-600">{dossier.remediation}</div>}
      </div>
    );
  }
  if (k === "tenant_connection") {
    const c = dossier.connection || {};
    return (
      <div className="space-y-0.5">
        <Row label="Tenant" value={c.tenant_id} />
        <Row label="Auth" value={c.auth_method} />
        <Row label="Status" value={c.status} />
      </div>
    );
  }
  return <pre className="whitespace-pre-wrap text-xs text-slate-500">{JSON.stringify(node.data, null, 2)}</pre>;
}

function driftLabel(d: string): string {
  if (d === "ok") return "Documented + live ✓";
  if (d === "documented_missing") return "Documented, not live ⚠";
  if (d === "live_uncontrolled") return "Live, undocumented ⛔";
  return d;
}

// ----------------------------------------------------------------- viz primitives
function scoreColor(score: number): string {
  if (score >= 80) return "#16a34a";
  if (score >= 60) return "#65a30d";
  if (score >= 40) return "#d97706";
  return "#dc2626";
}

function ScoreRing({ score }: { score: number | null | undefined }) {
  if (score == null) {
    return <div className="flex h-14 w-14 items-center justify-center rounded-full border-4 border-slate-200 text-xs text-slate-400">—</div>;
  }
  const r = 24;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, score));
  const col = scoreColor(pct);
  return (
    <svg width="56" height="56" viewBox="0 0 56 56" className="shrink-0">
      <circle cx="28" cy="28" r={r} fill="none" stroke="#e2e8f0" strokeWidth="6" />
      <circle
        cx="28" cy="28" r={r} fill="none" stroke={col} strokeWidth="6" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c * (1 - pct / 100)} transform="rotate(-90 28 28)"
      />
      <text x="28" y="31" textAnchor="middle" fontSize="14" fontWeight="700" fill={col}>{pct}</text>
    </svg>
  );
}

function RiskBar({ passed, failed, na }: { passed?: number; failed?: number; na?: number }) {
  const p = Math.max(0, passed || 0), f = Math.max(0, failed || 0), n = Math.max(0, na || 0);
  const total = p + f + n;
  if (total === 0) return null;
  const pct = (x: number) => `${(x / total) * 100}%`;
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
      <div style={{ width: pct(p), backgroundColor: "#16a34a" }} />
      <div style={{ width: pct(f), backgroundColor: "#dc2626" }} />
      <div style={{ width: pct(n), backgroundColor: "#cbd5e1" }} />
    </div>
  );
}

const CHIP_TONE: Record<string, string> = {
  slate: "bg-slate-100 text-slate-600",
  green: "bg-emerald-100 text-emerald-700",
  red: "bg-red-100 text-red-700",
  amber: "bg-amber-100 text-amber-700",
  orange: "bg-orange-100 text-orange-700",
};

function Chip({ children, tone = "slate" }: { children: ReactNode; tone?: string }) {
  return <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${CHIP_TONE[tone] || CHIP_TONE.slate}`}>{children}</span>;
}

function sevTone(sev: string): string {
  const s = (sev || "").toLowerCase();
  if (s === "critical" || s === "high" || s === "error") return "red";
  if (s === "medium" || s === "warning") return "amber";
  return "slate";
}
function critTone(c: string): string {
  const s = (c || "").toLowerCase();
  if (s === "critical") return "red";
  if (s === "high") return "orange";
  if (s === "medium") return "amber";
  return "slate";
}
