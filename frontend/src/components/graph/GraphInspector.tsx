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

const CTA: { key: string; label: string; link: string }[] = [
  { key: "inventory", label: "Inventory", link: "/inventory" },
  { key: "architecture", label: "Architecture", link: "" },
  { key: "memory", label: "Memory", link: "" },
  { key: "assessment", label: "Assessment", link: "" },
  { key: "change", label: "Change Explorer", link: "/change-explorer" },
  { key: "rbac", label: "RBAC", link: "/rbac" },
  { key: "telemetry", label: "Telemetry Intel", link: "/telemetry-intel" },
  { key: "backupdr", label: "Backup & DR", link: "/backupdr" },
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

        {/* Workload CTA grid (Phase 2) */}
        {isWorkload && dossier && (
          <div className="mt-4">
            <div className="mb-1.5 text-[11px] font-semibold uppercase text-slate-400">Open in</div>
            <div className="grid grid-cols-2 gap-1.5">
              {CTA.map((c) => {
                const href = links[c.key] || c.link;
                if (!href) return null;
                return (
                  <button
                    key={c.key}
                    onClick={() => navigate(href)}
                    className="rounded-md border border-slate-200 px-2 py-1 text-left text-xs text-slate-600 hover:bg-slate-50"
                  >
                    {c.label} →
                  </button>
                );
              })}
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
        <div className="space-y-0.5">
          <Row label="Type" value={node.data.workload_type} />
          <Row label="Environment" value={node.data.environment} />
          <Row label="Criticality" value={node.data.criticality} />
          <Row label="Resources" value={dossier.member_resources} />
        </div>
        {risk && (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
            <div className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Latest assessment</div>
            <Row label="Score" value={risk.score != null ? `${risk.score}/100` : "—"} />
            <Row label="Failing" value={risk.failed} />
            <Row label="Worst severity" value={risk.severity} />
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
