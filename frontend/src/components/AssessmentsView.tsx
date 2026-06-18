import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { Markdown as LazyMarkdown } from "./LazyMarkdown";
import {
  api,
  API_BASE,
  type AssessmentCheckMeta,
  type AssessmentComplianceFramework,
  type AssessmentFinding,
  type AssessmentFindingStateT,
  type AssessmentPillarScore,
  type AssessmentPortfolioRow,
  type AssessmentRunDetail,
  type AssessmentRunSummary,
  type AssessmentScannedResource,
  type Workload,
} from "../api";
import { formatError, formatTimestamp } from "../utils/format";
import { CopyButton } from "./CopyButton";
import { AzureIcon, friendlyResourceType } from "./AzureIcon";
import { PillarRadar } from "./PillarRadar";

// Render AI-authored text (executive summary, per-finding impact) as Markdown so **bold**,
// lists, etc. display formatted instead of raw. Styling is via arbitrary variants (this app
// has no @tailwindcss/typography plugin). `inline` flows within a label line.
function Markdown({ children, className = "", inline = false }: { children: string; className?: string; inline?: boolean }) {
  if (inline) {
    return (
      <span className={`[&_p]:m-0 [&_p]:inline [&_strong]:font-semibold [&_code]:rounded [&_code]:bg-black/5 [&_code]:px-1 ${className}`}>
        <LazyMarkdown>{children}</LazyMarkdown>
      </span>
    );
  }
  return (
    <div
      className={
        "[&_p]:my-0 [&_p+p]:mt-2 [&_strong]:font-semibold " +
        "[&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-0.5 " +
        "[&_code]:rounded [&_code]:bg-black/5 [&_code]:px-1 [&_code]:text-[0.9em] [&_a]:text-brand [&_a]:underline " +
        className
      }
    >
      <LazyMarkdown>{children}</LazyMarkdown>
    </div>
  );
}

const SEV_META: Record<string, { label: string; cls: string; rank: number }> = {
  critical: { label: "Critical", cls: "bg-red-100 text-red-700", rank: 3 },
  error: { label: "Error", cls: "bg-orange-100 text-orange-700", rank: 2 },
  warning: { label: "Warning", cls: "bg-amber-100 text-amber-700", rank: 1 },
  info: { label: "Info", cls: "bg-sky-100 text-sky-700", rank: 0 },
};
const STATUS_META: Record<string, { label: string; cls: string }> = {
  pass: { label: "Pass", cls: "bg-green-100 text-green-700" },
  fail: { label: "Fail", cls: "bg-red-100 text-red-700" },
  not_applicable: { label: "N/A", cls: "bg-gray-100 text-gray-500" },
  error: { label: "Error", cls: "bg-purple-100 text-purple-700" },
  manual: { label: "Manual", cls: "bg-indigo-100 text-indigo-700" },
  waived: { label: "Waived", cls: "bg-indigo-100 text-indigo-700" },
};
const STATE_META: Record<string, { label: string; cls: string }> = {
  open: { label: "Open", cls: "bg-gray-100 text-gray-600" },
  in_progress: { label: "In progress", cls: "bg-blue-100 text-blue-700" },
  resolved: { label: "Resolved", cls: "bg-green-100 text-green-700" },
  waived: { label: "Waived", cls: "bg-indigo-100 text-indigo-700" },
  risk_accepted: { label: "Risk accepted", cls: "bg-amber-100 text-amber-700" },
};
const PILLAR_META: Record<string, { label: string; icon: string }> = {
  security: { label: "Security", icon: "🛡️" },
  reliability: { label: "Reliability", icon: "🔄" },
  cost: { label: "Cost Optimization", icon: "💰" },
  operations: { label: "Operational Excellence", icon: "⚙️" },
  performance: { label: "Performance Efficiency", icon: "⚡" },
};
// Compact pillar labels for the findings toggle bar (full names are long).
const PILLAR_SHORT: Record<string, string> = {
  security: "Security",
  reliability: "Reliability",
  cost: "Cost",
  operations: "Operations",
  performance: "Performance",
};
// Fixed display order for the status toggle bar (worst → least).
const STATUS_ORDER = ["fail", "error", "manual", "waived", "pass", "not_applicable"] as const;

// The full set of Well-Architected pillars offered when running/scheduling an assessment.
const ALL_PILLARS = ["security", "reliability", "cost", "operations", "performance"] as const;

// Recognised Well-Architected methodologies → the pillar bundle each one runs. Selecting a
// pack is a one-click way to launch a WARA / WASA / full WAF review.
const PACK_PRESETS: { id: string; short: string; label: string; icon: string; pillars: string[] }[] = [
  { id: "waf", short: "WAF", label: "Well-Architected Review", icon: "🏛️", pillars: [...ALL_PILLARS] },
  { id: "wara", short: "WARA", label: "Reliability Assessment", icon: "🔄", pillars: ["reliability"] },
  { id: "wasa", short: "WASA", label: "Security Assessment", icon: "🛡️", pillars: ["security"] },
];

// Compliance frameworks a finding can map to, in display order, for the multi-select filter.
const FRAMEWORK_ORDER = ["cis", "nist", "iso", "mcsb", "pci"] as const;
type FrameworkKey = (typeof FRAMEWORK_ORDER)[number];
const FRAMEWORK_LABEL: Record<FrameworkKey, string> = {
  cis: "CIS",
  nist: "NIST",
  iso: "ISO 27001",
  mcsb: "MCSB",
  pci: "PCI DSS",
};
// The frameworks a single finding is mapped to (those with at least one control id).
function findingFrameworks(f: AssessmentFinding): FrameworkKey[] {
  return FRAMEWORK_ORDER.filter((k) => (f.frameworks[k]?.length ?? 0) > 0);
}

const RUN_STATUS_META: Record<string, { label: string; cls: string; spin?: boolean }> = {
  queued: { label: "Queued", cls: "bg-gray-100 text-gray-600" },
  running: { label: "Running", cls: "bg-blue-100 text-blue-700", spin: true },
  succeeded: { label: "Succeeded", cls: "bg-green-100 text-green-700" },
  failed: { label: "Failed", cls: "bg-red-100 text-red-700" },
  cancelled: { label: "Cancelled", cls: "bg-amber-100 text-amber-700" },
};
function RunStatusBadge({ status }: { status: string }) {
  const m = RUN_STATUS_META[status] ?? { label: status, cls: "bg-gray-100 text-gray-600" };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${m.cls}`}>
      {m.spin && <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />}
      {m.label}
    </span>
  );
}

/** Azure portal deep-link to a resource's Overview blade, from its ARM resource id. */
function portalUrl(resourceId: string): string {
  return `https://portal.azure.com/#@/resource${resourceId}/overview`;
}


// Score color bands (admin-tunable via Settings → Assessments & Architecture). Defaults
// match the shipped values; refreshed from the catalog when AssessmentsPanel mounts.
let SCORE_GOOD = 80;
let SCORE_WARN = 50;

function scoreColor(s: number | null | undefined): string {
  if (s == null) return "text-gray-400";
  if (s >= SCORE_GOOD) return "text-green-600";
  if (s >= SCORE_WARN) return "text-amber-600";
  return "text-red-600";
}
function scoreBg(s: number | null | undefined): string {
  if (s == null) return "bg-gray-200 text-gray-500";
  if (s >= SCORE_GOOD) return "bg-green-100 text-green-700";
  if (s >= SCORE_WARN) return "bg-amber-100 text-amber-700";
  return "bg-red-100 text-red-700";
}
function scoreRing(s: number | null | undefined): string {
  if (s == null) return "#d1d5db";
  if (s >= SCORE_GOOD) return "#16a34a";
  if (s >= SCORE_WARN) return "#d97706";
  return "#dc2626";
}

function TrustBar({ run }: { run: AssessmentRunSummary }) {
  // Surfaces the run's result confidence so a partial / throttled run is never mistaken for
  // a clean pass. Confidence comes from how many applicable controls were actually evaluated.
  const conf = (run.confidence || run.totals?.confidence || "").toLowerCase();
  const completeness = run.completeness_pct ?? run.totals?.completeness_pct;
  const errored = run.totals?.errored ?? 0;
  const manual = run.totals?.manual ?? 0;
  const worst = run.worst_case_score;
  const packMeta = PACK_PRESETS.find((p) => p.id === run.trigger);
  const confTone =
    conf === "high" ? "border-green-200 bg-green-50 text-green-700"
      : conf === "medium" ? "border-amber-200 bg-amber-50 text-amber-700"
        : conf === "low" ? "border-red-200 bg-red-50 text-red-700"
          : "border-gray-200 bg-gray-50 text-gray-600";
  if (!conf && completeness == null && !packMeta) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
      {packMeta && (
        <span className="rounded-full border border-brand/30 bg-brand/5 px-2 py-0.5 font-medium text-brand" title={packMeta.label}>
          {packMeta.icon} {packMeta.short}
        </span>
      )}
      {conf && (
        <span className={`rounded-full border px-2 py-0.5 font-medium ${confTone}`}
          title="Result confidence = share of applicable controls that were actually evaluated (not errored).">
          {conf === "high" ? "✓ High confidence" : conf === "medium" ? "◐ Medium confidence" : "▲ Low confidence"}
          {completeness != null ? ` · ${completeness}% evaluated` : ""}
        </span>
      )}
      {errored > 0 && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-700"
          title="Controls that could not be evaluated (auth/throttle/timeout). Excluded from the optimistic score.">
          {errored} not evaluated
        </span>
      )}
      {worst != null && run.overall_score != null && worst !== run.overall_score && (
        <span className="rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-gray-600"
          title="Worst-case score assumes every un-evaluated control would have failed.">
          worst-case {worst}/100
        </span>
      )}
      {manual > 0 && (
        <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-indigo-700"
          title="Manual controls awaiting a reviewer attestation — excluded from the score until answered.">
          {manual} manual pending
        </span>
      )}
      {run.catalog_version && <span className="text-gray-300">catalog {run.catalog_version}</span>}
    </div>
  );
}

function ScoreGauge({ score, size = 120, label }: { score: number | null; size?: number; label?: string }) {
  const r = size / 2 - 8;
  const c = 2 * Math.PI * r;
  const pct = score == null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  return (
    <div className="flex flex-col items-center">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e5e7eb" strokeWidth="8" />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={scoreRing(score)} strokeWidth="8" strokeLinecap="round"
          strokeDasharray={`${c * pct} ${c}`} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
        <text x="50%" y="50%" dominantBaseline="central" textAnchor="middle" className={`font-bold ${scoreColor(score)}`}
          style={{ fontSize: size * 0.26, fill: "currentColor" }}>{score == null ? "—" : score}</text>
      </svg>
      {label && <div className="mt-1 text-xs font-medium text-gray-600">{label}</div>}
    </div>
  );
}

function Sparkline({ values, width = 110, height = 28 }: { values: number[]; width?: number; height?: number }) {
  if (!values.length) return <span className="text-[11px] text-gray-300">—</span>;
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 100);
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  const pts = values.map((v, i) => `${i * step},${height - ((v - min) / span) * height}`).join(" ");
  const last = values[values.length - 1];
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={pts} fill="none" stroke={scoreRing(last)} strokeWidth="1.5" />
      <circle cx={(values.length - 1) * step} cy={height - ((last - min) / span) * height} r="2" fill={scoreRing(last)} />
    </svg>
  );
}

function SeverityChip({ severity }: { severity: string }) {
  const m = SEV_META[severity] ?? SEV_META.info;
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${m.cls}`}>{m.label}</span>;
}
function StatusChip({ status }: { status: string }) {
  const m = STATUS_META[status] ?? STATUS_META.error;
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${m.cls}`}>{m.label}</span>;
}

function PillarCard({ pillar, score, active, onClick }: { pillar: string; score: AssessmentPillarScore; active?: boolean; onClick?: () => void }) {
  const meta = PILLAR_META[pillar] ?? { label: pillar, icon: "📋" };
  return (
    <button
      type="button"
      onClick={onClick}
      title={onClick ? `Filter controls to ${meta.label}` : undefined}
      className={`flex items-center gap-3 rounded-lg border bg-white px-4 py-3 text-left transition ${
        onClick ? "cursor-pointer hover:border-brand/50 hover:shadow-sm" : "cursor-default"
      } ${active ? "border-brand ring-1 ring-brand/40" : ""}`}
    >
      <ScoreGauge score={score.score} size={72} />
      <div>
        <div className="flex items-center gap-1.5 text-sm font-medium text-gray-800"><span>{meta.icon}</span>{meta.label}</div>
        <div className="mt-0.5 text-xs text-gray-500">
          {score.passed} passed · {score.failed} failed{score.na ? ` · ${score.na} N/A` : ""}{score.waived ? ` · ${score.waived} waived` : ""}
        </div>
      </div>
    </button>
  );
}

// ---------------- Compliance coverage view ----------------
function ComplianceView({ compliance }: { compliance: Record<string, AssessmentComplianceFramework> }) {
  const frameworks = Object.entries(compliance).filter(([, f]) => f.total > 0);
  if (frameworks.length === 0) return <p className="text-sm text-gray-400">No framework-mapped controls were applicable.</p>;
  return (
    <div className="space-y-4">
      {frameworks.map(([key, f]) => (
        <div key={key} className="rounded-lg border bg-white p-4">
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-2 font-medium text-gray-800"><span>{f.icon}</span>{f.label}</div>
            <span className={`rounded px-2 py-0.5 text-xs font-semibold ${scoreBg(f.coverage)}`}>{f.coverage ?? "—"}% coverage</span>
          </div>
          <div className="mb-2 h-2 w-full overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full" style={{ width: `${f.coverage ?? 0}%`, background: scoreRing(f.coverage) }} />
          </div>
          <div className="text-xs text-gray-500">{f.passed} of {f.total} controls passing · {f.failed} failing</div>
          <div className="mt-2 flex flex-wrap gap-1">
            {f.controls.map((ctrl) => (
              <span key={ctrl.control}
                title={ctrl.checks.map((x) => `${x.title} (${x.status})`).join("\n")}
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                  ctrl.status === "pass" ? "bg-green-50 text-green-700"
                  : ctrl.status === "not_applicable" ? "bg-gray-50 text-gray-400"
                  : "bg-red-50 text-red-700"}`}>
                {ctrl.status === "pass" ? "✓" : ctrl.status === "not_applicable" ? "·" : "✗"} {ctrl.control}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------- Scanned resources view ----------------
function ResourcesView({ resources, totalCount }: { resources: AssessmentScannedResource[]; totalCount: number | null }) {
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");

  // Group by ARM type for the type facet + section headers.
  const byType = useMemo(() => {
    const m: Record<string, AssessmentScannedResource[]> = {};
    for (const r of resources) (m[r.type.toLowerCase()] ??= []).push(r);
    return Object.entries(m)
      .map(([t, list]) => ({ type: t, label: friendlyResourceType(t), list }))
      .sort((a, b) => b.list.length - a.list.length || a.label.localeCompare(b.label));
  }, [resources]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return resources.filter((r) => {
      if (typeFilter !== "all" && r.type.toLowerCase() !== typeFilter) return false;
      if (q) {
        const hay = `${r.name} ${r.type} ${r.resource_group} ${r.location}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [resources, query, typeFilter]);

  if (resources.length === 0) {
    return (
      <p className="text-sm text-gray-400">
        {totalCount && totalCount > 0
          ? `${totalCount} resources were scanned, but the inventory wasn't captured for this run. Re-run the assessment to populate the resource list.`
          : "No resource inventory was captured for this run. Re-run the assessment to populate the resource list."}
      </p>
    );
  }

  const capped = typeof totalCount === "number" && totalCount > resources.length;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search resources, type, RG, region…"
          className="w-64 rounded-md border px-2.5 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-brand" />
        <span className="text-[11px] text-gray-400">
          {rows.length} of {resources.length} shown{capped ? ` · ${totalCount} total scanned` : ""}
        </span>
      </div>
      {/* Type facet */}
      <div className="flex flex-wrap gap-1.5">
        <button onClick={() => setTypeFilter("all")}
          className={`rounded-full border px-2.5 py-1 text-[11px] ${typeFilter === "all" ? "border-brand bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
          All types ({resources.length})
        </button>
        {byType.map((g) => (
          <button key={g.type} onClick={() => setTypeFilter(g.type)}
            className={`flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] ${typeFilter === g.type ? "border-brand bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
            <AzureIcon kind="resource" type={g.type} className="h-3.5 w-3.5" />
            {g.label} ({g.list.length})
          </button>
        ))}
      </div>
      {/* Resource table */}
      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-3 py-2 font-medium">Resource</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Resource group</th>
              <th className="px-3 py-2 font-medium">Region</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id || `${r.resource_group}/${r.name}`} className="border-t hover:bg-gray-50">
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <AzureIcon kind="resource" type={r.type} className="h-4 w-4" />
                    {r.id ? (
                      <a
                        href={portalUrl(r.id)}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Open in Azure Portal"
                        className="group inline-flex items-center gap-1 font-medium text-gray-800 hover:text-brand hover:underline"
                      >
                        {r.name || "—"}
                        <span className="text-gray-300 transition group-hover:text-brand">↗</span>
                      </a>
                    ) : (
                      <span className="font-medium text-gray-800">{r.name || "—"}</span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2 text-gray-600">{friendlyResourceType(r.type)}</td>
                <td className="px-3 py-2 text-gray-500">{r.resource_group || "—"}</td>
                <td className="px-3 py-2 text-gray-500">{r.location || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------- Findings table ----------------
function FindingsTable({
  runId, workloadId, connectionId, findings, states, onChanged,
  pillar, onPillarChange, statusFilter, onStatusChange,
}: {
  pillar: string;
  onPillarChange: (p: string) => void;
  statusFilter: string;
  onStatusChange: (s: string) => void;
  runId: string;
  workloadId: string;
  connectionId: string | null;
  findings: AssessmentFinding[];
  states: Record<string, AssessmentFindingStateT>;
  onChanged: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );
  // Findings that already have a planned Azure Policy guardrail (the bridge "loop closed").
  const linksQ = useQuery({
    queryKey: ["policyEnforcementLinks", workloadId],
    queryFn: () => api.policyEnforcementLinks(workloadId),
    retry: false,
  });
  const plannedChecks = useMemo(
    () => new Set((linksQ.data?.links ?? []).map((l) => l.check_id)),
    [linksQ.data],
  );
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ col: string; dir: "asc" | "desc" }>({ col: "status", dir: "asc" });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Multi-select framework filter (OR semantics): empty = no framework filtering.
  const [frameworkFilter, setFrameworkFilter] = useState<Set<FrameworkKey>>(new Set());
  function toggleFramework(k: FrameworkKey) {
    setFrameworkFilter((p) => { const n = new Set(p); n.has(k) ? n.delete(k) : n.add(k); return n; });
  }
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ id: string; text: string; ok: boolean } | null>(null);
  const [waiveFor, setWaiveFor] = useState<string | null>(null);
  // Multi-select cart for "Plan enforcement in Azure Policy".
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Bulk-action panel over the selected findings (state / assign / waive / ticket).
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkMsg, setBulkMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [bulkWaive, setBulkWaive] = useState(false);
  const [bulkAssignee, setBulkAssignee] = useState("");

  // A finding is enforceable as a policy if it FAILED and targets concrete resource types
  // (the backend resolves its detection predicate from the check id).
  const eligible = (f: AssessmentFinding) => f.status === "fail" && (f.resource_types?.length ?? 0) > 0;

  function toggleSel(id: string) {
    setSelected((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }
  function planEnforcement() {
    const chosen = findings.filter((f) => selected.has(f.check_id) && eligible(f));
    if (chosen.length === 0) return;
    const handoff = {
      source: "assessment" as const,
      run_id: runId,
      workload_id: workloadId,
      connection_id: connectionId,
      findings: chosen.map((f) => ({
        check_id: f.check_id,
        title: f.title,
        description: f.description,
        severity: f.severity,
        pillar: f.pillar,
        frameworks: f.frameworks,
        remediation: f.remediation,
        remediation_command: f.remediation_command,
        resource_types: f.resource_types,
        flagged_count: f.flagged_count,
        flagged_resources: (f.flagged_resources ?? []).slice(0, 25).map((r) => ({
          id: r.id, name: r.name, type: r.type, resourceGroup: r.resource_group,
        })),
        suggested_effect: "deny",
      })),
    };
    try {
      sessionStorage.setItem("policyHandoff", JSON.stringify(handoff));
      // Scope the Policy panel to this workload. The panel reads + persists this key, so the scope
      // survives a refresh and a later "Clear scope" sticks (it isn't re-applied from the hand-off).
      sessionStorage.setItem("policyWorkloadId", workloadId);
    } catch { /* ignore */ }
    navigate("/policy/rollout");
  }

  function toggle(id: string) {
    setExpanded((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }
  function toggleSort(col: string) {
    setSort((s) => (s.col === col ? { col, dir: s.dir === "asc" ? "desc" : "asc" } : { col, dir: "asc" }));
  }

  const STATUS_RANK: Record<string, number> = { fail: 0, error: 1, manual: 2, waived: 3, pass: 4, not_applicable: 5 };
  // A finding matches the framework filter if it's mapped to ANY of the selected frameworks
  // (multi-select OR); an empty selection matches everything.
  const matchesFrameworks = (f: AssessmentFinding) =>
    frameworkFilter.size === 0 || findingFrameworks(f).some((k) => frameworkFilter.has(k));
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = findings.filter((f) => {
      if (pillar !== "all" && f.pillar !== pillar) return false;
      if (statusFilter !== "all" && f.status !== statusFilter) return false;
      if (!matchesFrameworks(f)) return false;
      if (q) {
        const hay = `${f.title} ${f.description} ${(f.frameworks.cis || []).join(" ")} ${(f.frameworks.nist || []).join(" ")} ${(f.frameworks.iso || []).join(" ")} ${(f.frameworks.mcsb || []).join(" ")} ${(f.frameworks.pci || []).join(" ")}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    list = [...list].sort((a, b) => {
      let cmp = 0;
      if (sort.col === "status") cmp = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9);
      else if (sort.col === "severity") cmp = (SEV_META[b.severity]?.rank ?? 0) - (SEV_META[a.severity]?.rank ?? 0);
      else if (sort.col === "pillar") cmp = a.pillar.localeCompare(b.pillar);
      else if (sort.col === "title") cmp = a.title.localeCompare(b.title);
      else if (sort.col === "flagged") cmp = a.flagged_count - b.flagged_count;
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return list;
  }, [findings, query, pillar, statusFilter, frameworkFilter, sort]);

  const arrow = (col: string) => (sort.col === col ? (sort.dir === "asc" ? "▲" : "▼") : "↕");

  // Faceted counts for the pillar/status/framework toggle bars: each dimension is counted
  // independently of its own selection (but respects the OTHER filters + search), and the
  // button sets are taken from the full run so they don't appear/disappear while filtering.
  const facet = useMemo(() => {
    const qq = query.trim().toLowerCase();
    const searched = qq
      ? findings.filter((f) => `${f.title} ${f.description} ${(f.frameworks.cis || []).join(" ")} ${(f.frameworks.nist || []).join(" ")} ${(f.frameworks.iso || []).join(" ")} ${(f.frameworks.mcsb || []).join(" ")} ${(f.frameworks.pci || []).join(" ")}`.toLowerCase().includes(qq))
      : findings;
    // Pillar + status counts respect the framework filter (so they reflect the active scope);
    // the framework counts do NOT apply their own selection (standard multi-select facet).
    const forPillars = searched.filter((f) => (statusFilter === "all" || f.status === statusFilter) && matchesFrameworks(f));
    const forStatuses = searched.filter((f) => (pillar === "all" || f.pillar === pillar) && matchesFrameworks(f));
    const forFrameworks = searched.filter((f) => (pillar === "all" || f.pillar === pillar) && (statusFilter === "all" || f.status === statusFilter));
    const pillarCounts: Record<string, number> = {};
    for (const f of forPillars) pillarCounts[f.pillar] = (pillarCounts[f.pillar] ?? 0) + 1;
    const statusCounts: Record<string, number> = {};
    for (const f of forStatuses) statusCounts[f.status] = (statusCounts[f.status] ?? 0) + 1;
    const frameworkCounts: Record<string, number> = {};
    for (const f of forFrameworks) for (const k of findingFrameworks(f)) frameworkCounts[k] = (frameworkCounts[k] ?? 0) + 1;
    return {
      pillarCounts,
      statusCounts,
      frameworkCounts,
      pillarTotal: forPillars.length,
      statusTotal: forStatuses.length,
      pillarsPresent: ALL_PILLARS.filter((p) => findings.some((f) => f.pillar === p)),
      statusesPresent: STATUS_ORDER.filter((s) => findings.some((f) => f.status === s)),
      frameworksPresent: FRAMEWORK_ORDER.filter((k) => findings.some((f) => (f.frameworks[k]?.length ?? 0) > 0)),
    };
  }, [findings, query, pillar, statusFilter, frameworkFilter]);

  async function setState(checkId: string, patch: Partial<{ status: string; assignee: string }>) {
    setBusy(checkId);
    try {
      await api.updateAssessmentFindingState({ workload_id: workloadId, check_id: checkId, ...patch });
      onChanged();
    } catch (e) {
      setMsg({ id: checkId, text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }
  async function ticket(checkId: string, connectorId: string) {
    setBusy(checkId);
    setMsg(null);
    try {
      const r = await api.createAssessmentTicket({ run_id: runId, check_id: checkId, connector_id: connectorId });
      setMsg({ id: checkId, ok: r.ok, text: r.ok ? `Ticket created: ${r.ticket_id || r.ticket_url || "ok"}` : `Failed: ${r.detail}` });
      onChanged();
    } catch (e) {
      setMsg({ id: checkId, text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }
  async function waive(checkId: string, justification: string, approver: string, expires: string) {
    setBusy(checkId);
    try {
      // Parse the date input (YYYY-MM-DD) as LOCAL midnight, not UTC midnight, so the waiver
      // expires at the day the user intended in their own timezone.
      let expiresIso: string | null = null;
      if (expires) {
        const [y, m, d] = expires.split("-").map(Number);
        expiresIso = new Date(y, (m || 1) - 1, d || 1).toISOString();
      }
      await api.createAssessmentWaiver({
        workload_id: workloadId, check_id: checkId, justification, approver,
        expires_at: expiresIso,
      });
      setWaiveFor(null);
      qc.invalidateQueries({ queryKey: ["assessmentWaivers", workloadId] });
      onChanged();
    } catch (e) {
      setMsg({ id: checkId, text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  // --- Bulk actions over the currently-selected findings ---------------------------
  // Each runs the matching single-finding API for every selected check id and reports an
  // aggregate "N/total applied" result; partial failures are tolerated and counted.
  async function runBulk(label: string, fn: (checkId: string) => Promise<unknown>, opts?: { invalidateWaivers?: boolean }) {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkMsg(null);
    let ok = 0;
    let failed = 0;
    for (const id of ids) {
      try { await fn(id); ok++; } catch { failed++; }
    }
    if (opts?.invalidateWaivers) qc.invalidateQueries({ queryKey: ["assessmentWaivers", workloadId] });
    setBulkBusy(false);
    setBulkMsg({ ok: failed === 0, text: `${label}: ${ok}/${ids.length} applied${failed ? ` · ${failed} failed` : ""}.` });
    onChanged();
  }
  function bulkSetState(status: string) {
    return runBulk(`Set state → ${STATE_META[status]?.label ?? status}`, (id) =>
      api.updateAssessmentFindingState({ workload_id: workloadId, check_id: id, status }));
  }
  function bulkAssign() {
    const a = bulkAssignee.trim();
    if (!a) return;
    return runBulk(`Assigned to ${a}`, (id) =>
      api.updateAssessmentFindingState({ workload_id: workloadId, check_id: id, assignee: a }));
  }
  function bulkTicket(connectorId: string) {
    const conn = ticketConnectors.find((c) => c.id === connectorId);
    // Creating tickets hits an external system (Jira/ServiceNow) — confirm before fan-out.
    if (!window.confirm(`Create ${selected.size} ticket${selected.size === 1 ? "" : "s"} in ${conn?.name ?? "the connector"}?`)) return;
    return runBulk("Tickets created", (id) =>
      api.createAssessmentTicket({ run_id: runId, check_id: id, connector_id: connectorId }));
  }
  async function bulkWaiveSubmit(justification: string, approver: string, expires: string) {
    let expiresIso: string | null = null;
    if (expires) {
      const [y, m, d] = expires.split("-").map(Number);
      expiresIso = new Date(y, (m || 1) - 1, d || 1).toISOString();
    }
    await runBulk("Waivers applied", (id) =>
      api.createAssessmentWaiver({ workload_id: workloadId, check_id: id, justification, approver, expires_at: expiresIso }),
      { invalidateWaivers: true });
    setBulkWaive(false);
  }
  function clearSelection() {
    setSelected(new Set());
    setBulkOpen(false);
    setBulkWaive(false);
    setBulkMsg(null);
  }

  return (
    <div>
      {selected.size > 0 && (
        <div className="sticky top-0 z-10 mb-2 rounded-lg border border-brand/40 bg-brand/5 px-3 py-2 shadow-sm">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm font-medium text-gray-800">🚦 {selected.size} finding{selected.size === 1 ? "" : "s"} selected</span>
            <button
              onClick={() => { setBulkOpen((o) => !o); setBulkMsg(null); }}
              className={`rounded-lg border px-3 py-1.5 text-xs font-semibold ${bulkOpen ? "border-brand bg-brand/10 text-brand" : "border-brand/40 bg-white text-brand hover:bg-brand/10"}`}
            >
              Bulk actions {bulkOpen ? "▴" : "▾"}
            </button>
            <button onClick={planEnforcement} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand/90">
              Plan enforcement in Azure Policy →
            </button>
            <button onClick={clearSelection} className="text-xs text-gray-500 hover:text-gray-700">Clear</button>
            <span className="ml-auto text-[11px] text-gray-500">Apply a state, owner, waiver or ticket to all selected — or bring them to the Safe-Rollout Planner.</span>
          </div>
          {bulkOpen && (
            <div className="mt-2 border-t border-brand/20 pt-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] font-medium text-gray-600">Apply to {selected.size}:</span>
                {/* Set state */}
                <select
                  defaultValue=""
                  disabled={bulkBusy}
                  onChange={(e) => { const v = e.target.value; e.currentTarget.value = ""; if (v) void bulkSetState(v); }}
                  className="rounded-md border px-2 py-1 text-[11px] disabled:opacity-50"
                >
                  <option value="">Set state…</option>
                  {Object.entries(STATE_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                </select>
                {/* Assign to */}
                <div className="flex items-center gap-1">
                  <input
                    value={bulkAssignee}
                    onChange={(e) => setBulkAssignee(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") void bulkAssign(); }}
                    placeholder="Assign to…"
                    disabled={bulkBusy}
                    className="w-32 rounded-md border px-2 py-1 text-[11px] disabled:opacity-50"
                  />
                  <button onClick={() => void bulkAssign()} disabled={bulkBusy || !bulkAssignee.trim()}
                    className="rounded-md border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50">Assign</button>
                </div>
                {/* Waive */}
                <button onClick={() => setBulkWaive((w) => !w)} disabled={bulkBusy}
                  className="rounded-md border border-indigo-200 px-2 py-1 text-[11px] text-indigo-600 hover:bg-indigo-50 disabled:opacity-50">Waive…</button>
                {/* Ticket */}
                {ticketConnectors.length > 0 && (
                  <select
                    defaultValue=""
                    disabled={bulkBusy}
                    onChange={(e) => { const v = e.target.value; e.currentTarget.value = ""; if (v) void bulkTicket(v); }}
                    className="rounded-md border px-2 py-1 text-[11px] disabled:opacity-50"
                  >
                    <option value="">Create tickets…</option>
                    {ticketConnectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.type})</option>)}
                  </select>
                )}
                {bulkBusy && <span className="text-[11px] text-gray-500">Applying…</span>}
                {bulkMsg && <span className={`text-[11px] ${bulkMsg.ok ? "text-green-600" : "text-red-600"}`}>{bulkMsg.text}</span>}
              </div>
              {bulkWaive && (
                <WaiveForm onCancel={() => setBulkWaive(false)} onSubmit={(j, a, e) => void bulkWaiveSubmit(j, a, e)} busy={bulkBusy} />
              )}
            </div>
          )}
        </div>
      )}
      <div className="mb-2 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search controls, frameworks…"
            className="w-56 rounded-md border px-2.5 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-brand" />
          <span className="text-[11px] text-gray-400">{rows.length} controls</span>
          {findings.some(eligible) && (
            <button
              onClick={() => {
                // Operate on the currently-filtered eligible rows, but PRESERVE selections made
                // under other filters (selection persists across filter changes).
                const elig = rows.filter(eligible).map((f) => f.check_id);
                setSelected((p) => {
                  if (elig.every((id) => p.has(id))) {
                    const next = new Set(p);
                    for (const id of elig) next.delete(id);
                    return next;
                  }
                  return new Set([...p, ...elig]);
                });
              }}
              className="text-[11px] text-brand hover:underline"
            >
              {rows.filter(eligible).every((f) => selected.has(f.check_id)) && rows.some(eligible) ? "Deselect all" : "Select all failing"}
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Pillar toggle bar */}
          <div className="inline-flex overflow-hidden rounded-md border text-xs">
            <button onClick={() => onPillarChange("all")} className={`px-2.5 py-1 ${pillar === "all" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All ({facet.pillarTotal})</button>
            {facet.pillarsPresent.map((p) => (
              <button key={p} onClick={() => onPillarChange(p)} className={`border-l px-2.5 py-1 ${pillar === p ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
                {PILLAR_META[p].icon} {PILLAR_SHORT[p]} ({facet.pillarCounts[p] ?? 0})
              </button>
            ))}
          </div>
          {/* Status toggle bar */}
          <div className="inline-flex overflow-hidden rounded-md border text-xs">
            <button onClick={() => onStatusChange("all")} className={`px-2.5 py-1 ${statusFilter === "all" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All ({facet.statusTotal})</button>
            {facet.statusesPresent.map((s) => (
              <button key={s} onClick={() => onStatusChange(s)} className={`border-l px-2.5 py-1 ${statusFilter === s ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
                {STATUS_META[s].label} ({facet.statusCounts[s] ?? 0})
              </button>
            ))}
          </div>
          {/* Framework multi-select bar (OR; pick several to combine) */}
          {facet.frameworksPresent.length > 0 && (
            <div className="inline-flex overflow-hidden rounded-md border text-xs" title="Filter by compliance framework (select multiple to combine)">
              <button onClick={() => setFrameworkFilter(new Set())} className={`px-2.5 py-1 ${frameworkFilter.size === 0 ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All frameworks</button>
              {facet.frameworksPresent.map((k) => (
                <button key={k} onClick={() => toggleFramework(k)} className={`border-l px-2.5 py-1 ${frameworkFilter.has(k) ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
                  {FRAMEWORK_LABEL[k]} ({facet.frameworkCounts[k] ?? 0})
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 text-left text-gray-500">
            <tr className="border-b">
              <th className="w-6 py-2 pl-3" />
              <th className="w-6 py-2" title="Select to plan policy enforcement" />
              <th onClick={() => toggleSort("status")} className="cursor-pointer select-none py-2 pr-3 font-medium">Status <span className="text-gray-300">{arrow("status")}</span></th>
              <th onClick={() => toggleSort("title")} className="cursor-pointer select-none py-2 pr-3 font-medium">Control <span className="text-gray-300">{arrow("title")}</span></th>
              <th onClick={() => toggleSort("pillar")} className="cursor-pointer select-none py-2 pr-3 font-medium">Pillar <span className="text-gray-300">{arrow("pillar")}</span></th>
              <th onClick={() => toggleSort("severity")} className="cursor-pointer select-none py-2 pr-3 font-medium">Severity <span className="text-gray-300">{arrow("severity")}</span></th>
              <th className="py-2 pr-3 font-medium">Owner</th>
              <th onClick={() => toggleSort("flagged")} className="cursor-pointer select-none py-2 pr-3 font-medium">Flagged <span className="text-gray-300">{arrow("flagged")}</span></th>
              <th className="py-2 pr-3 font-medium">Frameworks</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((f) => {
              const isOpen = expanded.has(f.check_id);
              const st = states[f.check_id];
              return (
                <>
                  <tr key={f.check_id} onClick={() => toggle(f.check_id)} className="cursor-pointer border-b last:border-0 hover:bg-gray-50">
                    <td className="py-2 pl-3 text-gray-400">{isOpen ? "▾" : "▸"}</td>
                    <td className="py-2" onClick={(e) => e.stopPropagation()}>
                      {eligible(f) ? (
                        <input
                          type="checkbox"
                          checked={selected.has(f.check_id)}
                          onChange={() => toggleSel(f.check_id)}
                          title="Select to plan policy enforcement"
                          className="h-3.5 w-3.5 cursor-pointer accent-brand"
                        />
                      ) : null}
                    </td>
                    <td className="py-2 pr-3"><StatusChip status={f.status} /></td>
                    <td className="py-2 pr-3 font-medium text-gray-800">
                      {f.title}
                      {plannedChecks.has(f.check_id) && (
                        <span className="ml-1.5 rounded bg-green-100 px-1 py-0.5 text-[9px] font-medium text-green-700" title="An Azure Policy guardrail is planned for this finding">🛡 guardrail planned</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-gray-600">{PILLAR_META[f.pillar]?.icon} {PILLAR_META[f.pillar]?.label ?? f.pillar}</td>
                    <td className="py-2 pr-3"><SeverityChip severity={f.severity} /></td>
                    <td className="py-2 pr-3">
                      {st ? (
                        <span className="flex items-center gap-1">
                          <span className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${STATE_META[st.status]?.cls ?? ""}`}>{STATE_META[st.status]?.label ?? st.status}</span>
                          {st.assignee && <span className="text-[10px] text-gray-500">{st.assignee}</span>}
                          {st.ticket_id && <span className="text-[9px] text-blue-600">🎫{st.ticket_id}</span>}
                        </span>
                      ) : <span className="text-gray-300">—</span>}
                    </td>
                    <td className="py-2 pr-3 text-gray-700">{f.status === "fail" ? (f.partial ? `${f.flagged_count}+` : f.flagged_count) : "—"}</td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap gap-1">
                        {f.profile && <span title={`CIS profile ${f.profile === "L1" ? "Level 1 (baseline)" : "Level 2 (defense-in-depth)"}`} className={`rounded px-1 py-0.5 text-[9px] font-semibold ${f.profile === "L1" ? "bg-amber-100 text-amber-700" : "bg-orange-100 text-orange-700"}`}>{f.profile}</span>}
                        {(f.frameworks.cis || []).map((x) => <span key={`cis-${x}`} className="rounded bg-indigo-50 px-1 py-0.5 text-[9px] text-indigo-600">{x}</span>)}
                        {(f.frameworks.nist || []).map((x) => <span key={`nist-${x}`} className="rounded bg-teal-50 px-1 py-0.5 text-[9px] text-teal-600">NIST {x}</span>)}
                        {(f.frameworks.iso || []).map((x) => <span key={`iso-${x}`} className="rounded bg-purple-50 px-1 py-0.5 text-[9px] text-purple-600">ISO {x}</span>)}
                        {(f.frameworks.mcsb || []).map((x) => <span key={`mcsb-${x}`} className="rounded bg-sky-50 px-1 py-0.5 text-[9px] text-sky-600">MCSB {x}</span>)}
                        {(f.frameworks.pci || []).map((x) => <span key={`pci-${x}`} className="rounded bg-rose-50 px-1 py-0.5 text-[9px] text-rose-600">{x}</span>)}
                      </div>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="border-b bg-gray-50/60">
                      <td />
                      <td colSpan={8} className="px-3 py-3">
                        <p className="text-gray-600">{f.description}</p>
                        {f.ai_rationale && <div className="mt-1.5 rounded bg-blue-50 px-2 py-1.5 text-blue-700"><span className="font-medium">Impact:</span> <Markdown inline className="text-blue-700">{f.ai_rationale}</Markdown></div>}
                        {f.status === "waived" && f.waiver && (
                          <p className="mt-1.5 rounded bg-indigo-50 px-2 py-1.5 text-indigo-700">
                            <span className="font-medium">Waived:</span> {f.waiver.justification}{f.waiver.approver ? ` — approved by ${f.waiver.approver}` : ""}
                          </p>
                        )}
                        {f.status === "fail" && f.flagged_resources.length > 0 && (
                          <div className="mt-2">
                            <div className="mb-1 flex items-center justify-between">
                              <span className="font-medium text-gray-700">Flagged resources{f.flagged_count > f.flagged_resources.length ? ` (showing ${f.flagged_resources.length} of ${f.flagged_count})` : ""}:</span>
                              {f.flagged_resources.some((r) => r.remediation_command) && (
                                <CopyButton
                                  content={() => f.flagged_resources.filter((r) => r.remediation_command).map((r) => r.remediation_command).join("\n")}
                                  label="Copy all fix commands"
                                  title="Copy every per-resource remediation command"
                                  className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50"
                                />
                              )}
                            </div>
                            <div className="max-h-56 space-y-1 overflow-y-auto rounded border bg-white p-1.5">
                              {f.flagged_resources.map((r) => (
                                <div key={r.id} className="rounded border-b border-gray-100 px-1 py-1 last:border-0">
                                  <div className="flex items-center gap-2 text-[11px]">
                                    {r.id ? (
                                      <a href={portalUrl(r.id)} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
                                        className="inline-flex items-center gap-1 font-mono text-blue-600 hover:underline" title="Open in Azure portal">
                                        {r.name}
                                        <svg className="h-2.5 w-2.5 opacity-70" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 3h7v7M21 3l-9 9M5 5h6M5 5v14h14v-6" strokeLinecap="round" strokeLinejoin="round" /></svg>
                                      </a>
                                    ) : <span className="font-mono text-gray-800">{r.name}</span>}
                                    <span className="text-gray-400">·</span>
                                    <span className="text-gray-500">{r.resource_group}</span>
                                    <span className="text-gray-300">{friendlyResourceType(r.type)}</span>
                                  </div>
                                  {r.remediation_command && (
                                    <div className="mt-1 flex items-start gap-1">
                                      <pre className="min-w-0 flex-1 overflow-x-auto rounded bg-gray-900 px-2 py-1 font-mono text-[11px] text-gray-100">{r.remediation_command}</pre>
                                      <CopyButton content={r.remediation_command} title="Copy fix command" className="mt-0.5 shrink-0 rounded border px-1 py-1 text-gray-500 hover:bg-gray-50" />
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {f.status === "error" && f.error && <p className="mt-2 rounded bg-purple-50 px-2 py-1.5 text-purple-700">Query error: {f.error}</p>}
                        {f.status === "manual" && <p className="mt-2 rounded bg-indigo-50 px-2 py-1.5 text-indigo-700">Manual control — record an attestation (Settings → workload) for it to count toward the score.</p>}
                        {f.status === "fail" && f.partial && <p className="mt-2 rounded bg-amber-50 px-2 py-1.5 text-amber-700">Very large result set — the count is accurate, but only a capped sample of the matching resources is listed here.</p>}
                        <div className="mt-2 rounded border border-gray-200 bg-white px-2 py-1.5">
                          <span className="font-medium text-gray-700">Remediation:</span> <span className="text-gray-600">{f.remediation}</span>
                          {f.remediation_command && (
                            <div className="mt-1 flex items-center justify-between gap-2">
                              <span className="text-[10px] text-gray-400">Template (placeholders filled per resource above):</span>
                            </div>
                          )}
                          {f.remediation_command && <pre className="mt-0.5 overflow-x-auto rounded bg-gray-800 px-2 py-1 font-mono text-[11px] text-gray-300">{f.remediation_command}</pre>}
                        </div>
                        {/* Workflow actions */}
                        {(f.status === "fail" || f.status === "error") && (
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <select value={st?.status ?? "open"} onChange={(e) => void setState(f.check_id, { status: e.target.value })}
                              disabled={busy === f.check_id} className="rounded-md border px-2 py-1 text-[11px]">
                              {Object.entries(STATE_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                            </select>
                            <input defaultValue={st?.assignee ?? ""} placeholder="Assign to…"
                              disabled={busy === f.check_id}
                              onBlur={(e) => { if (e.target.value !== (st?.assignee ?? "")) void setState(f.check_id, { assignee: e.target.value }); }}
                              className="w-32 rounded-md border px-2 py-1 text-[11px] disabled:opacity-50" />
                            <button onClick={() => setWaiveFor(waiveFor === f.check_id ? null : f.check_id)}
                              className="rounded-md border border-indigo-200 px-2 py-1 text-[11px] text-indigo-600 hover:bg-indigo-50">Waive</button>
                            {ticketConnectors.length > 0 && (
                              <select defaultValue="" onChange={(e) => { if (e.target.value) void ticket(f.check_id, e.target.value); e.currentTarget.value = ""; }}
                                disabled={busy === f.check_id} className="rounded-md border px-2 py-1 text-[11px]">
                                <option value="">Create ticket…</option>
                                {ticketConnectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.type})</option>)}
                              </select>
                            )}
                            {st?.ticket_url && <a href={st.ticket_url} target="_blank" rel="noreferrer" className="text-[11px] text-blue-600 underline">Open ticket</a>}
                            {msg && msg.id === f.check_id && <span className={`text-[11px] ${msg.ok ? "text-green-600" : "text-red-600"}`}>{msg.text}</span>}
                          </div>
                        )}
                        {waiveFor === f.check_id && (
                          <WaiveForm onCancel={() => setWaiveFor(null)} onSubmit={(j, a, e) => void waive(f.check_id, j, a, e)} busy={busy === f.check_id} />
                        )}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
            {rows.length === 0 && <tr><td colSpan={8} className="py-6 text-center text-gray-400">No matching controls.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function WaiveForm({ onCancel, onSubmit, busy }: { onCancel: () => void; onSubmit: (j: string, a: string, e: string) => void; busy: boolean }) {
  const [just, setJust] = useState("");
  const [approver, setApprover] = useState("");
  const [expires, setExpires] = useState("");
  return (
    <div className="mt-2 rounded-lg border border-indigo-200 bg-indigo-50/50 p-3">
      <div className="mb-1 text-[11px] font-medium text-indigo-700">Risk acceptance / waiver</div>
      <textarea value={just} onChange={(e) => setJust(e.target.value)} placeholder="Justification (required)…" rows={2}
        className="w-full rounded-md border px-2 py-1 text-[12px]" />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input value={approver} onChange={(e) => setApprover(e.target.value)} placeholder="Approver" className="w-40 rounded-md border px-2 py-1 text-[11px]" />
        <label className="text-[11px] text-gray-500">Expires: <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} className="rounded-md border px-2 py-1 text-[11px]" /></label>
        <button onClick={() => just.trim() && onSubmit(just, approver, expires)} disabled={!just.trim() || busy}
          className="rounded-md bg-indigo-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-indigo-700 disabled:opacity-50">{busy ? "Saving…" : "Accept risk"}</button>
        <button onClick={onCancel} className="rounded-md border px-3 py-1 text-[11px] text-gray-600 hover:bg-gray-50">Cancel</button>
      </div>
    </div>
  );
}

// ---------------- Run detail ----------------
function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const q = useQuery({
    queryKey: ["assessmentRun", runId],
    queryFn: () => api.assessmentRun(runId),
    // Poll while a (re-)run is still in flight so the report fills in live.
    refetchInterval: (query) => {
      const s = (query.state.data as { run?: AssessmentRunDetail } | undefined)?.run?.status;
      return s === "queued" || s === "running" ? 2500 : false;
    },
  });
  const run = q.data?.run as AssessmentRunDetail | undefined;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"findings" | "compliance" | "resources">("findings");
  const [busy, setBusy] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);
  const pdfAbortRef = useRef<AbortController | null>(null);
  // Findings filter state, lifted here so the score gauge + pillar tiles can drive it.
  const [pillar, setPillar] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  // Clicking a pillar tile / the overall gauge focuses the Controls tab and filters it.
  function focusPillar(p: string) {
    setTab("findings");
    setPillar((cur) => (cur === p ? "all" : p));
  }

  useEffect(() => () => pdfAbortRef.current?.abort(), []);

  const statesQ = useQuery({
    queryKey: ["assessmentFindingStates", run?.workload_id],
    queryFn: () => api.assessmentFindingStates(run!.workload_id),
    enabled: !!run?.workload_id,
  });
  const trendQ = useQuery({
    queryKey: ["assessmentTrend", run?.workload_id],
    queryFn: () => api.assessmentTrend(run!.workload_id),
    enabled: !!run?.workload_id,
  });

  if (q.isLoading) return <div className="p-8 text-sm text-gray-400">Loading assessment…</div>;
  if (!run) return <div className="p-8 text-sm text-red-600">Assessment run not found.</div>;
  const states = statesQ.data?.states ?? {};
  const trend = (trendQ.data?.points ?? []).map((p) => p.overall ?? 0);
  const isRunning = run.status === "queued" || run.status === "running";

  async function toggleBaseline() {
    setBusy(true);
    try {
      await api.setAssessmentBaseline(runId, !run!.is_baseline);
      qc.invalidateQueries({ queryKey: ["assessmentRun", runId] });
      qc.invalidateQueries({ queryKey: ["assessmentRuns"] });
      qc.invalidateQueries({ queryKey: ["assessmentPortfolio"] });
    } finally {
      setBusy(false);
    }
  }

  // Re-run the same assessment (same workload, pillars, connection, AI setting) and jump to
  // the fresh run so its progress streams in live.
  async function rerun() {
    if (!run) return;
    setBusy(true);
    try {
      const res = await api.enqueueAssessments({
        workload_ids: [run.workload_id],
        pillars: run.pillars,
        connection_id: run.connection_id ?? null,
        use_ai: run.used_ai,
      });
      qc.invalidateQueries({ queryKey: ["assessmentRuns"] });
      qc.invalidateQueries({ queryKey: ["assessmentPortfolio"] });
      const newId = res.runs?.[0]?.id;
      if (newId && newId !== runId) navigate(`/assessments/${newId}`);
    } finally {
      setBusy(false);
    }
  }
  function exportRun(fmt: "json" | "csv" | "pdf") {
    void (async () => {
      try {
        const res = await fetch(`${API_BASE}/assessments/runs/${runId}/export?format=${fmt}`, { credentials: "include" });
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const cd = res.headers.get("Content-Disposition") || "";
        const m = /filename="?([^";]+)"?/.exec(cd);
        a.download = m ? m[1] : `assessment-${runId}.${fmt}`;
        a.click();
        URL.revokeObjectURL(url);
      } catch {
        /* ignore */
      }
    })();
  }
  async function exportPdf() {
    if (pdfBusy) return;
    const controller = new AbortController();
    pdfAbortRef.current = controller;
    setPdfBusy(true);
    try {
      const res = await fetch(`${API_BASE}/assessments/runs/${runId}/export?format=pdf`, {
        credentials: "include",
        signal: controller.signal,
      });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename="?([^";]+)"?/.exec(cd);
      a.download = m ? m[1] : `assessment-${runId}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      if ((err as { name?: string } | null)?.name !== "AbortError") {
        /* ignore */
      }
    } finally {
      pdfAbortRef.current = null;
      setPdfBusy(false);
    }
  }
  function cancelPdfExport() {
    pdfAbortRef.current?.abort();
  }

  return (
    <div className="w-full space-y-5 px-4 py-4 sm:px-6 lg:px-8">
      <div className="flex w-full items-center justify-between gap-4">
        <button onClick={onBack} className="text-sm text-gray-500 hover:text-gray-700">← Back to assessments</button>
        <div className="flex items-center gap-2">
          <button onClick={() => void rerun()} disabled={busy || isRunning}
            className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
            title="Run this assessment again for the same workload, pillars & connection">
            {isRunning ? "⟳ Running…" : busy ? "⟳ Re-running…" : "⟳ Re-run"}
          </button>
          <button onClick={() => void toggleBaseline()} disabled={busy}
            className={`rounded-lg border px-2.5 py-1 text-xs ${run.is_baseline ? "border-amber-300 bg-amber-50 text-amber-700" : "text-gray-600 hover:bg-gray-50"}`}>
            {run.is_baseline ? "★ Baseline" : "☆ Set baseline"}
          </button>
          <button onClick={() => void exportPdf()} disabled={pdfBusy} className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50" title="Download a branded PDF report">{pdfBusy ? "Generating…" : "⬇ PDF"}</button>
          <button onClick={() => exportRun("csv")} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">⬇ CSV</button>
          <button onClick={() => exportRun("json")} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">⬇ JSON</button>
          <span className="text-xs text-gray-400">{formatTimestamp(run.started_at ?? undefined)}</span>
        </div>
      </div>

      <div>
        <h1 className="text-xl font-semibold text-gray-800">{run.workload_name} <span className="text-gray-400">— assessment</span></h1>
        <p className="mt-0.5 text-sm text-gray-500">
          {run.pillars.map((p) => PILLAR_META[p]?.label ?? p).join(" + ")} · {run.used_ai ? "AI-assisted" : "deterministic"}
          {run.trigger === "schedule" ? " · scheduled" : ""}
        </p>
        <TrustBar run={run} />
      </div>

      {isRunning && (
        <div className="flex items-center gap-3 rounded-lg border border-brand/30 bg-brand/5 p-3 text-sm">
          <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-brand/30 border-t-brand" />
          <span className="font-medium text-gray-700">
            {run.status === "queued" ? "Assessment queued…" : "Assessment running…"} this report updates automatically as checks complete.
          </span>
        </div>
      )}
      {run.status === "failed" && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          This assessment failed to complete. Try re-running it.
        </div>
      )}

      <div className="grid w-full gap-5 rounded-lg border bg-white p-5 2xl:grid-cols-[auto_minmax(0,1fr)_auto] 2xl:items-center">
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => { setTab("findings"); setPillar("all"); setStatusFilter("all"); }}
            title="Show all controls"
            className={`w-fit rounded-lg p-1 transition hover:bg-gray-50 ${pillar === "all" && statusFilter === "all" ? "ring-1 ring-brand/40" : ""}`}
          >
            <ScoreGauge score={run.overall_score} size={130} label="Overall" />
          </button>
          {Object.keys(run.scores).length >= 3 && (
            <div title="Score across the Well-Architected pillars">
              <PillarRadar scores={run.scores} size={188} showLegend={false} />
            </div>
          )}
        </div>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-3">
          {Object.entries(run.scores).map(([p, s]) => (
            <PillarCard key={p} pillar={p} score={s} active={pillar === p} onClick={() => focusPillar(p)} />
          ))}
        </div>
        <div className="text-right text-xs text-gray-500 2xl:justify-self-end">
          {trend.length > 1 && <div className="mb-1 flex justify-end"><Sparkline values={trend} /></div>}
          <div><span className="font-medium text-green-600">{run.totals.passed}</span> passed</div>
          <div><span className="font-medium text-red-600">{run.totals.failed}</span> failed</div>
          {run.totals.waived ? <div><span className="font-medium text-indigo-600">{run.totals.waived}</span> waived</div> : null}
          <div><span className="font-medium text-gray-400">{run.totals.na}</span> N/A</div>
          {typeof run.resource_count === "number" && (
            <button
              onClick={() => setTab("resources")}
              className="mt-1 block w-full text-right text-gray-500 hover:text-brand"
              title="View the resources scanned in this assessment"
            >
              <span className="font-medium text-gray-700">{run.resource_count}</span> resources scanned
            </button>
          )}
        </div>
      </div>

      {run.diff && ((run.diff.new_failures?.length ?? 0) > 0 || (run.diff.resolved?.length ?? 0) > 0) && (
        <div className="rounded-lg border bg-white p-4 text-sm">
          <div className="mb-1 font-medium text-gray-700">
            Change since {run.diff.baseline_is_pinned ? "pinned baseline" : "previous run"}
            {run.diff.new_criticals ? <span className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">{run.diff.new_criticals} new critical</span> : null}
          </div>
          {run.diff.new_failures.length > 0 && (
            <div className="text-red-600">▲ {run.diff.new_failures.length} new: {run.diff.new_failures.slice(0, 5).map((nf) => (typeof nf === "string" ? nf : nf.title)).join(", ")}</div>
          )}
          {run.diff.resolved.length > 0 && <div className="text-green-600">▼ {run.diff.resolved.length} resolved: {run.diff.resolved.slice(0, 5).join(", ")}</div>}
        </div>
      )}

      {run.summary && (
        <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-4">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-blue-500">Executive summary</div>
          <Markdown className="text-sm text-gray-700">{run.summary}</Markdown>
        </div>
      )}

      {pdfBusy && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-[1px]">
          <div className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-5 shadow-2xl">
            <div className="flex items-start gap-4">
              <div className="mt-0.5 h-10 w-10 animate-spin rounded-full border-4 border-brand/20 border-t-brand" />
              <div className="min-w-0 flex-1">
                <div className="text-lg font-semibold text-gray-900">Generating PDF report</div>
                <p className="mt-1 text-sm text-gray-500">
                  The report is being compiled with the table of contents, summary, findings, and appendix.
                  You can cancel while it is processing.
                </p>
                <div className="mt-4 flex items-center gap-2">
                  <button
                    onClick={cancelPdfExport}
                    className="rounded-lg border border-red-200 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100"
                  >
                    Cancel
                  </button>
                  <span className="text-xs text-gray-400">This only cancels the current PDF request.</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="flex gap-1 border-b">
        {(["findings", "compliance", "resources"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-sm ${tab === t ? "border-b-2 border-brand font-medium text-brand" : "text-gray-500 hover:text-gray-700"}`}>
            {t === "findings" ? "Controls" : t === "compliance" ? "Compliance" : `Resources${typeof run.resource_count === "number" ? ` (${run.resource_count})` : ""}`}
          </button>
        ))}
      </div>
      {tab === "findings" ? (
        <FindingsTable runId={runId} workloadId={run.workload_id} connectionId={run.connection_id ?? null} findings={run.findings} states={states}
          pillar={pillar} onPillarChange={setPillar} statusFilter={statusFilter} onStatusChange={setStatusFilter}
          onChanged={() => { qc.invalidateQueries({ queryKey: ["assessmentRun", runId] }); statesQ.refetch(); }} />
      ) : tab === "compliance" ? (
        <ComplianceView compliance={run.compliance ?? {}} />
      ) : (
        <ResourcesView resources={run.resources ?? []} totalCount={run.resource_count ?? null} />
      )}
    </div>
  );
}

// ---------------- Run flow ----------------
function RunFlow({ onQueued }: { onQueued: () => void }) {
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = (wlQ.data?.workloads ?? []) as Workload[];
  const [selectedWl, setSelectedWl] = useState<string[]>(() => {
    try { const pre = sessionStorage.getItem("azsup.assessWorkload"); if (pre) { sessionStorage.removeItem("azsup.assessWorkload"); return [pre]; } } catch { /* ignore */ }
    return [];
  });
  const [pillars, setPillars] = useState<string[]>([...ALL_PILLARS]);
  const [activePack, setActivePack] = useState<string>("waf");
  const [useAi, setUseAi] = useState(true);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const filtered = useMemo(
    () => workloads.filter((w) => w.name.toLowerCase().includes(query.trim().toLowerCase())),
    [workloads, query],
  );
  const allFilteredSelected = filtered.length > 0 && filtered.every((w) => selectedWl.includes(w.id));

  function toggleWl(id: string) { setSelectedWl((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])); }
  function toggleAll() {
    setSelectedWl((prev) => (allFilteredSelected ? prev.filter((id) => !filtered.some((w) => w.id === id)) : Array.from(new Set([...prev, ...filtered.map((w) => w.id)]))));
  }
  function togglePillar(p: string) { setActivePack(""); setPillars((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p])); }
  function selectPack(pk: { id: string; pillars: string[] }) { setActivePack(pk.id); setPillars([...pk.pillars]); }

  async function run() {
    if (selectedWl.length === 0 || pillars.length === 0) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const res = await api.enqueueAssessments({ workload_ids: selectedWl, pillars, pack: activePack || null, use_ai: useAi });
      setNotice(`Queued ${res.queued} assessment${res.queued === 1 ? "" : "s"} — running in the background. Track progress in History below.`);
      onQueued();
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border bg-white p-5">
      <h2 className="font-medium text-gray-800">Run new assessments</h2>
      <p className="mt-0.5 text-sm text-gray-500">Select one or more workloads and the assessment types to run. Each workload is queued as a background run (read-only Resource Graph queries); results score 0–100, map to CIS/NIST/ISO, and summarize with AI. Watch status in History below.</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs font-medium text-gray-600">Workloads {selectedWl.length > 0 && <span className="text-brand">({selectedWl.length} selected)</span>}</span>
            <button onClick={toggleAll} disabled={busy || filtered.length === 0} className="text-xs text-brand hover:underline disabled:opacity-50">{allFilteredSelected ? "Clear all" : "Select all"}</button>
          </div>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter workloads…" className="mb-2 w-full rounded-lg border px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand" />
          <div className="max-h-52 space-y-0.5 overflow-y-auto rounded-lg border bg-gray-50/60 p-1.5">
            {filtered.length === 0 && <div className="px-2 py-3 text-center text-xs text-gray-400">{wlQ.isLoading ? "Loading…" : "No workloads."}</div>}
            {filtered.map((w) => (
              <label key={w.id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-sm hover:bg-white">
                <input type="checkbox" checked={selectedWl.includes(w.id)} onChange={() => toggleWl(w.id)} disabled={busy} />
                <span className="truncate text-gray-700">{w.name}</span>
              </label>
            ))}
          </div>
        </div>
        <div>
          <span className="mb-1 block text-xs font-medium text-gray-600">Assessment pack</span>
          <div className="mb-3 flex flex-wrap gap-2">
            {PACK_PRESETS.map((pk) => (
              <button key={pk.id} onClick={() => selectPack(pk)} disabled={busy} title={pk.label}
                className={`rounded-lg border px-3 py-1.5 text-sm transition ${activePack === pk.id ? "border-brand bg-brand text-white font-medium" : "text-gray-600 hover:bg-gray-50"}`}>
                {pk.icon} {pk.short}
              </button>
            ))}
            <span className="self-center text-xs text-gray-400">{activePack ? PACK_PRESETS.find((p) => p.id === activePack)?.label : "Custom selection"}</span>
          </div>
          <span className="mb-1 block text-xs font-medium text-gray-600">Pillars {pillars.length > 0 && <span className="text-brand">({pillars.length})</span>}</span>
          <div className="flex flex-wrap gap-2">
            {ALL_PILLARS.map((p) => (
              <button key={p} onClick={() => togglePillar(p)} disabled={busy}
                className={`rounded-lg border px-3 py-1.5 text-sm transition ${pillars.includes(p) ? "border-brand bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-50"}`}>{PILLAR_META[p].icon} {PILLAR_META[p].label}</button>
            ))}
          </div>
          <button onClick={() => { const all = pillars.length === ALL_PILLARS.length; setActivePack(all ? "" : "waf"); setPillars(all ? [] : [...ALL_PILLARS]); }} disabled={busy} className="mt-2 text-xs text-brand hover:underline disabled:opacity-50">{pillars.length === ALL_PILLARS.length ? "Clear all" : "Select all types"}</button>
          <label className="mt-2 flex items-center gap-2 text-xs text-gray-600">
            <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} disabled={busy} />AI executive summary &amp; impact
          </label>
        </div>
      </div>
      {error && <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}
      {notice && <div className="mt-3 rounded-md border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-700">{notice}</div>}
      <div className="mt-4 flex items-center gap-3">
        <button onClick={() => void run()} disabled={busy || selectedWl.length === 0 || pillars.length === 0}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50">
          {busy ? "Queuing…" : selectedWl.length > 1 ? `Run ${selectedWl.length} assessments` : "Run assessment"}
        </button>
        {selectedWl.length > 0 && pillars.length > 0 && <span className="text-xs text-gray-400">{selectedWl.length} workload{selectedWl.length === 1 ? "" : "s"} × {pillars.length} type{pillars.length === 1 ? "" : "s"}</span>}
      </div>
    </div>
  );
}

function RunHistory({ runs, onOpen, onDelete, onCancel }: { runs: AssessmentRunSummary[]; onOpen: (id: string) => void; onDelete: (id: string) => void; onCancel: (id: string) => void }) {
  const [groupBy, setGroupBy] = useState<"none" | "workload" | "status">("workload");
  // "all" = full grouped history; "latest" = one row per workload (its newest run).
  const [view, setView] = useState<"all" | "latest">("all");
  // Recency window in days (0 = all time). Active runs are always kept regardless.
  const [windowDays, setWindowDays] = useState<0 | 7 | 30 | 90>(0);
  // Per-group open/closed override (key → bool). Unset keys fall back to the busy heuristic.
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});

  // Window filter first — applies to both views. Always keep in-progress runs visible.
  const windowed = useMemo(() => {
    if (!windowDays) return runs;
    const cutoff = Date.now() - windowDays * 86_400_000;
    return runs.filter((r) => {
      if (r.status === "queued" || r.status === "running") return true;
      const t = r.started_at ? new Date(r.started_at).getTime() : 0;
      return t >= cutoff;
    });
  }, [runs, windowDays]);

  // Latest run per workload (newest by start time) — drives the "Latest per workload" view.
  const latestPerWorkload = useMemo(() => {
    const map = new Map<string, AssessmentRunSummary>();
    for (const r of windowed) {
      const prev = map.get(r.workload_id);
      const t = r.started_at ? new Date(r.started_at).getTime() : 0;
      const pt = prev?.started_at ? new Date(prev.started_at).getTime() : -1;
      if (!prev || t > pt) map.set(r.workload_id, r);
    }
    return Array.from(map.values());
  }, [windowed]);

  const groups = useMemo(() => {
    if (groupBy === "none") return [{ key: "", label: "", runs: windowed }];
    if (groupBy === "status") {
      const order = ["running", "queued", "failed", "cancelled", "succeeded"];
      const map = new Map<string, AssessmentRunSummary[]>();
      for (const r of windowed) { const k = r.status || "succeeded"; (map.get(k) ?? map.set(k, []).get(k)!).push(r); }
      return order.filter((k) => map.has(k)).map((k) => ({ key: k, label: RUN_STATUS_META[k]?.label ?? k, runs: map.get(k)! }));
    }
    // by workload
    const map = new Map<string, { name: string; runs: AssessmentRunSummary[] }>();
    for (const r of windowed) {
      const g = map.get(r.workload_id) ?? map.set(r.workload_id, { name: r.workload_name, runs: [] }).get(r.workload_id)!;
      g.runs.push(r);
    }
    return Array.from(map.entries())
      .sort((a, b) => a[1].name.localeCompare(b[1].name))
      .map(([key, v]) => ({ key, label: v.name, runs: v.runs }));
  }, [windowed, groupBy]);

  if (runs.length === 0) return <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-8 text-center text-sm text-gray-500">No assessments run yet.</div>;

  const active = windowed.filter((r) => r.status === "queued" || r.status === "running").length;

  // Collapse groups by default once the history gets busy — many workloads or lots of
  // runs would otherwise stack into a wall of tables. Small histories stay expanded.
  const busy = groups.length > 3 || windowed.length > 12;
  const isOpen = (key: string) => openMap[key] ?? !busy;
  const toggle = (key: string) => setOpenMap((m) => ({ ...m, [key]: !(m[key] ?? !busy) }));
  const expandAll = () => setOpenMap(Object.fromEntries(groups.map((g) => [g.key || "all", true])));
  const collapseAll = () => setOpenMap(Object.fromEntries(groups.map((g) => [g.key || "all", false])));
  // Latest 3 within each grouped section, with a "show older" disclosure (no limit when
  // grouping is "None" or in the latest-per-workload view, which are already compact).
  const groupLimit = groupBy === "none" ? 0 : 3;

  const segBtn = (activeSel: boolean) =>
    `px-2.5 py-1 text-xs transition ${activeSel ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-gray-500">
          {view === "latest"
            ? <>{latestPerWorkload.length} workload{latestPerWorkload.length === 1 ? "" : "s"}</>
            : <>{windowed.length} run{windowed.length === 1 ? "" : "s"}{windowDays ? <span className="text-gray-400"> of {runs.length}</span> : null}</>}
          {active > 0 && <span className="ml-2 text-blue-600">· {active} in progress</span>}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-gray-500">
          {/* Recency window */}
          <div className="inline-flex items-center gap-1.5">
            <span>Window:</span>
            <div className="inline-flex overflow-hidden rounded-md border">
              {([[7, "7d"], [30, "30d"], [90, "90d"], [0, "All"]] as const).map(([d, lbl]) => (
                <button key={lbl} onClick={() => setWindowDays(d)} className={segBtn(windowDays === d)}>{lbl}</button>
              ))}
            </div>
          </div>
          {/* View: full history vs latest-per-workload */}
          <div className="inline-flex items-center gap-1.5">
            <span>View:</span>
            <div className="inline-flex overflow-hidden rounded-md border">
              <button onClick={() => setView("all")} className={segBtn(view === "all")}>All runs</button>
              <button onClick={() => setView("latest")} className={segBtn(view === "latest")} title="One row per workload — its newest run">Latest per workload</button>
            </div>
          </div>
          {view === "all" && groupBy !== "none" && groups.length > 1 && (
            <div className="inline-flex items-center gap-1">
              <button onClick={expandAll} className="rounded px-1.5 py-0.5 hover:bg-gray-100">Expand all</button>
              <span className="text-gray-300">·</span>
              <button onClick={collapseAll} className="rounded px-1.5 py-0.5 hover:bg-gray-100">Collapse all</button>
            </div>
          )}
          {view === "all" && (
            <div className="inline-flex items-center gap-1.5">
              <span>Group by:</span>
              <div className="inline-flex overflow-hidden rounded-md border">
                {(["workload", "status", "none"] as const).map((g) => (
                  <button key={g} onClick={() => setGroupBy(g)} className={segBtn(groupBy === g)}>
                    {g === "workload" ? "Workload" : g === "status" ? "Status" : "None"}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {windowed.length === 0 ? (
        <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-8 text-center text-sm text-gray-500">No runs in the last {windowDays} days. <button onClick={() => setWindowDays(0)} className="font-medium text-brand hover:underline">Show all</button></div>
      ) : view === "latest" ? (
        <RunGroup key="latest" label="" runs={latestPerWorkload} open onToggle={() => {}} limit={0} onOpen={onOpen} onDelete={onDelete} onCancel={onCancel} />
      ) : (
        groups.map((grp) => {
          const key = grp.key || "all";
          return (
            <RunGroup key={key} label={grp.label} runs={grp.runs} open={isOpen(key)} onToggle={() => toggle(key)} limit={groupLimit} onOpen={onOpen} onDelete={onDelete} onCancel={onCancel} />
          );
        })
      )}
    </div>
  );
}

function RunGroup({ label, runs, open, onToggle, limit = 0, onOpen, onDelete, onCancel }: { label: string; runs: AssessmentRunSummary[]; open: boolean; onToggle: () => void; limit?: number; onOpen: (id: string) => void; onDelete: (id: string) => void; onCancel: (id: string) => void }) {
  // Column sort: click a header to sort, click again to flip direction.
  type RunSortKey = "workload" | "status" | "score" | "failed" | "when";
  const [sortKey, setSortKey] = useState<RunSortKey>("when");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  // When a group has more than `limit` runs, show only the top `limit` (latest, by the
  // current sort) and tuck the rest behind a "Show N older runs" disclosure.
  const [showAll, setShowAll] = useState(false);
  const done = (r: AssessmentRunSummary) => r.status === "succeeded" || r.status === "failed" || r.status === "cancelled";
  const cancellable = (r: AssessmentRunSummary) => r.status === "queued" || r.status === "running";

  // Always show the table when there's no group header (the "None" grouping) — otherwise
  // a collapsed state would leave the user with no way to re-open it.
  const showTable = open || !label;

  // Header summary so a collapsed group still tells you its latest state at a glance:
  // latest score (colored), a score trend sparkline, failed count, last-run time, and any
  // in-progress runs — no need to expand to know how a workload is doing.
  const summary = useMemo(() => {
    const t = (r: AssessmentRunSummary) => (r.started_at ? new Date(r.started_at).getTime() : 0);
    const byTimeDesc = [...runs].sort((a, b) => t(b) - t(a));
    const completedAsc = runs
      .filter((r) => r.status === "succeeded" && r.overall_score != null)
      .sort((a, b) => t(a) - t(b));
    const latestCompleted = completedAsc[completedAsc.length - 1];
    return {
      trend: completedAsc.map((r) => r.overall_score as number),
      latestScore: latestCompleted?.overall_score ?? null,
      latestFailed: latestCompleted ? latestCompleted.totals.failed : null,
      latestSeverity: latestCompleted?.severity ?? "info",
      activeCount: runs.filter((r) => r.status === "queued" || r.status === "running").length,
      lastWhen: byTimeDesc[0]?.started_at ?? null,
    };
  }, [runs]);

  function toggleSort(key: RunSortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "workload" || key === "status" ? "asc" : "desc"); }
  }

  const sortedRuns = useMemo(() => {
    const val = (r: AssessmentRunSummary): string | number => {
      switch (sortKey) {
        case "workload": return (r.workload_name || "").toLowerCase();
        case "status": return r.status || "";
        case "score": return r.overall_score ?? -1;
        case "failed": return r.status === "succeeded" ? (r.totals.failed ?? 0) : -1;
        case "when": return r.started_at ? new Date(r.started_at).getTime() : 0;
      }
    };
    const dir = sortDir === "asc" ? 1 : -1;
    return [...runs].sort((a, b) => {
      const av = val(a);
      const bv = val(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [runs, sortKey, sortDir]);

  const limited = limit > 0 && !showAll && sortedRuns.length > limit;
  const visibleRuns = limited ? sortedRuns.slice(0, limit) : sortedRuns;
  const hiddenCount = sortedRuns.length - visibleRuns.length;

  const SortTh = ({ k, label: thLabel, className = "" }: { k: RunSortKey; label: string; className?: string }) => (
    <th
      onClick={() => toggleSort(k)}
      className={`cursor-pointer select-none font-medium hover:text-gray-700 ${className}`}
      title={`Sort by ${thLabel}`}
    >
      <span className="inline-flex items-center gap-1">
        {thLabel}
        <span className={`text-[9px] ${sortKey === k ? "text-brand" : "text-gray-300"}`}>{sortKey === k ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
      </span>
    </th>
  );

  return (
    <div className="overflow-hidden rounded-lg border bg-white">
      {label && (
        <button onClick={onToggle} className="flex w-full items-center gap-2 border-b bg-gray-50 px-4 py-2 text-left text-sm text-gray-700 hover:bg-gray-100">
          <span className={`shrink-0 text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
          <span className="truncate font-medium">{label}</span>
          <span className="ml-0.5 shrink-0 rounded-full bg-gray-200 px-1.5 text-[10px] text-gray-600">{runs.length}</span>
          {/* At-a-glance summary, right-aligned — meaningful even while collapsed. */}
          <span className="ml-auto flex shrink-0 items-center gap-3 text-xs">
            {summary.activeCount > 0 && (
              <span className="inline-flex items-center gap-1 text-blue-600">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />{summary.activeCount} running
              </span>
            )}
            {summary.trend.length > 1 && <span className="hidden sm:inline-flex"><Sparkline values={summary.trend} width={60} height={18} /></span>}
            {summary.latestScore != null && (
              <span className="inline-flex items-baseline gap-1">
                <span className="text-[10px] text-gray-400">latest</span>
                <span className={`font-semibold ${scoreColor(summary.latestScore)}`}>{summary.latestScore}</span>
              </span>
            )}
            {summary.latestFailed != null && summary.latestFailed > 0 && (
              <span className="hidden items-center gap-1 text-gray-500 md:inline-flex">
                <SeverityChip severity={summary.latestSeverity} /> {summary.latestFailed}
              </span>
            )}
            {summary.lastWhen && <span className="hidden text-gray-400 lg:inline">{formatTimestamp(summary.lastWhen)}</span>}
          </span>
        </button>
      )}
      {showTable && (
        <table className="w-full text-sm">
          <thead className="bg-gray-50/60 text-left text-gray-500"><tr className="border-b">
            <SortTh k="workload" label="Workload" className="py-2 pl-4" />
            <SortTh k="status" label="Status" className="py-2 pr-3" />
            <SortTh k="score" label="Score" className="py-2 pr-3" />
            <th className="py-2 pr-3 font-medium">Pillars</th>
            <SortTh k="failed" label="Failed" className="py-2 pr-3" />
            <SortTh k="when" label="When" className="py-2 pr-3" />
            <th className="py-2 pr-3" />
          </tr></thead>
          <tbody>
            {visibleRuns.map((r) => {
              const clickable = r.status === "succeeded" || r.status === "failed";
              return (
                <tr key={r.id} className={`border-b last:border-0 ${clickable ? "cursor-pointer hover:bg-gray-50" : "opacity-90"}`} onClick={() => clickable && onOpen(r.id)}>
                  <td className="py-2 pl-4 font-medium text-gray-800">{r.is_baseline ? "★ " : ""}{r.workload_name}</td>
                  <td className="py-2 pr-3"><RunStatusBadge status={r.status} /></td>
                  <td className={`py-2 pr-3 font-semibold ${scoreColor(r.overall_score)}`}>{done(r) ? (r.overall_score ?? "—") : "—"}</td>
                  <td className="py-2 pr-3 text-gray-500">{r.pillars.map((p) => PILLAR_META[p]?.icon ?? "").join(" ")}</td>
                  <td className="py-2 pr-3">{r.status === "succeeded" && r.totals.failed > 0 ? <span className="inline-flex items-center gap-1.5"><SeverityChip severity={r.severity} /> {r.totals.failed}</span> : r.status === "succeeded" ? <span className="text-green-600">0</span> : <span className="text-gray-300">—</span>}</td>
                  <td className="py-2 pr-3 text-gray-400">{formatTimestamp(r.started_at ?? undefined)}</td>
                  <td className="py-2 pr-3 text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      {cancellable(r) && <button onClick={(e) => { e.stopPropagation(); onCancel(r.id); }} className="rounded border border-amber-200 px-2 py-0.5 text-xs text-amber-700 hover:bg-amber-50">Cancel</button>}
                      <button onClick={(e) => { e.stopPropagation(); onDelete(r.id); }} className="rounded border border-red-200 px-2 py-0.5 text-xs text-red-600 hover:bg-red-50">Delete</button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {limit > 0 && sortedRuns.length > limit && (
              <tr className="border-b last:border-0 bg-gray-50/40">
                <td colSpan={7} className="py-1.5 text-center">
                  <button
                    onClick={(e) => { e.stopPropagation(); setShowAll((v) => !v); }}
                    className="text-xs font-medium text-brand hover:underline"
                  >
                    {showAll ? "Show fewer ▴" : `Show ${hiddenCount} older run${hiddenCount === 1 ? "" : "s"} ▾`}
                  </button>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------- Portfolio heatmap ----------------
function PortfolioView({ onOpen }: { onOpen: (runId: string) => void }) {
  const q = useQuery({ queryKey: ["assessmentPortfolio"], queryFn: api.assessmentPortfolio });
  const rows = q.data?.workloads ?? [];
  const pillars = [...ALL_PILLARS];
  if (q.isLoading) return <div className="text-sm text-gray-400">Loading…</div>;
  if (rows.length === 0) return <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-8 text-center text-sm text-gray-500">No assessed workloads yet. Run an assessment to populate the portfolio.</div>;
  return (
    <div className="overflow-x-auto rounded-lg border bg-white">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-gray-500"><tr className="border-b">
          <th className="py-2 pl-4 font-medium">Workload</th><th className="py-2 pr-3 font-medium">Overall</th>
          {pillars.map((p) => <th key={p} className="py-2 pr-3 font-medium">{PILLAR_META[p]?.icon} {PILLAR_META[p]?.label}</th>)}
          <th className="py-2 pr-3 font-medium">Failed</th><th className="py-2 pr-3 font-medium">Trend</th><th className="py-2 pr-3 font-medium">Last run</th>
        </tr></thead>
        <tbody>
          {rows.map((w: AssessmentPortfolioRow) => (
            <tr key={w.workload_id} className="cursor-pointer border-b last:border-0 hover:bg-gray-50" onClick={() => onOpen(w.run_id)}>
              <td className="py-2 pl-4 font-medium text-gray-800">{w.workload_name}</td>
              <td className="py-2 pr-3"><span className={`rounded px-2 py-0.5 text-xs font-semibold ${scoreBg(w.overall_score)}`}>{w.overall_score ?? "—"}</span></td>
              {pillars.map((p) => <td key={p} className="py-2 pr-3"><span className={`rounded px-2 py-0.5 text-xs font-medium ${scoreBg(w.scores[p])}`}>{w.scores[p] ?? "—"}</span></td>)}
              <td className="py-2 pr-3">{w.failed > 0 ? <span className="inline-flex items-center gap-1.5"><SeverityChip severity={w.severity} /> {w.failed}</span> : <span className="text-green-600">0</span>}</td>
              <td className="py-2 pr-3"><Sparkline values={w.sparkline} /></td>
              <td className="py-2 pr-3 text-gray-400">{formatTimestamp(w.at ?? undefined)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------- Custom checks ----------------
function CustomChecksView() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["assessmentCustomChecks"], queryFn: api.assessmentCustomChecks });
  const checks = q.data?.checks ?? [];
  const [editing, setEditing] = useState<Partial<AssessmentCheckMeta> | null>(null);
  const [goal, setGoal] = useState("");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");

  async function generate() {
    if (!goal.trim()) return;
    setGenerating(true); setError("");
    try {
      const r = await api.generateAssessmentCheck(goal);
      setEditing({ ...r.draft, enabled: true });
      setError("");
    } catch (e) { setError(formatError(e)); } finally { setGenerating(false); }
  }
  async function save() {
    if (!editing?.title) return;
    try {
      await api.upsertAssessmentCustomCheck(editing);
      setEditing(null); setGoal("");
      qc.invalidateQueries({ queryKey: ["assessmentCustomChecks"] });
      qc.invalidateQueries({ queryKey: ["assessmentCatalog"] });
    } catch (e) { setError(formatError(e)); }
  }
  async function del(id: string) {
    if (!window.confirm("Delete this custom check?")) return;
    try {
      await api.deleteAssessmentCustomCheck(id);
      qc.invalidateQueries({ queryKey: ["assessmentCustomChecks"] });
      qc.invalidateQueries({ queryKey: ["assessmentCatalog"] });
    } catch (e) { setError(formatError(e)); }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border bg-white p-4">
        <h3 className="font-medium text-gray-800">Generate a custom control with AI</h3>
        <p className="mt-0.5 text-xs text-gray-500">Describe an org-specific control; the AI drafts a Resource Graph query, severity, and framework mapping for you to review.</p>
        <div className="mt-2 flex gap-2">
          <input value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="e.g. Flag storage accounts without a 'CostCenter' tag"
            className="flex-1 rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand" />
          <button onClick={() => void generate()} disabled={generating || !goal.trim()}
            className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">{generating ? "Generating…" : "✨ Generate"}</button>
        </div>
      </div>

      {error && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}

      {editing && <CustomCheckEditor draft={editing} setDraft={setEditing} onSave={() => void save()} onCancel={() => setEditing(null)} />}

      <div className="flex items-center justify-between">
        <h3 className="font-medium text-gray-800">Custom controls ({checks.length})</h3>
        {!editing && <button onClick={() => setEditing({ pillar: "security", severity: "warning", enabled: true, frameworks: {} })}
          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">+ New control</button>}
      </div>
      <div className="space-y-2">
        {checks.map((c) => (
          <div key={c.id} className="flex items-center justify-between rounded-lg border bg-white px-4 py-2.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2"><span className="text-sm font-medium text-gray-800">{c.title}</span><SeverityChip severity={c.severity} />
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{PILLAR_META[c.pillar]?.label}</span>
                {!c.enabled && <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500">disabled</span>}</div>
              <div className="mt-0.5 truncate text-xs text-gray-500">{c.description}</div>
            </div>
            <div className="flex shrink-0 gap-1.5">
              <button onClick={() => setEditing(c)} className="rounded border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">Edit</button>
              <button onClick={() => void del(c.id)} className="rounded border border-red-200 px-2 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
            </div>
          </div>
        ))}
        {q.isLoading && <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-6 text-center text-sm text-gray-400">Loading custom controls…</div>}
        {!q.isLoading && checks.length === 0 && !editing && <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-6 text-center text-sm text-gray-500">No custom controls yet.</div>}
      </div>
    </div>
  );
}

function CustomCheckEditor({ draft, setDraft, onSave, onCancel }: { draft: Partial<AssessmentCheckMeta>; setDraft: (d: Partial<AssessmentCheckMeta>) => void; onSave: () => void; onCancel: () => void }) {
  const set = (p: Partial<AssessmentCheckMeta>) => setDraft({ ...draft, ...p });
  const inputCls = "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
  return (
    <div className="rounded-lg border border-brand/30 bg-brand/5 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block"><span className="mb-1 block text-xs font-medium text-gray-600">Title</span>
          <input value={draft.title ?? ""} onChange={(e) => set({ title: e.target.value })} className={inputCls} /></label>
        <div className="grid grid-cols-2 gap-2">
          <label className="block"><span className="mb-1 block text-xs font-medium text-gray-600">Pillar</span>
            <select value={draft.pillar ?? "security"} onChange={(e) => set({ pillar: e.target.value })} className={inputCls}>{ALL_PILLARS.map((p) => <option key={p} value={p}>{PILLAR_META[p].label}</option>)}</select></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-gray-600">Severity</span>
            <select value={draft.severity ?? "warning"} onChange={(e) => set({ severity: e.target.value as never })} className={inputCls}><option value="critical">Critical</option><option value="error">Error</option><option value="warning">Warning</option><option value="info">Info</option></select></label>
        </div>
      </div>
      <label className="mt-2 block"><span className="mb-1 block text-xs font-medium text-gray-600">Description</span>
        <input value={draft.description ?? ""} onChange={(e) => set({ description: e.target.value })} className={inputCls} /></label>
      <label className="mt-2 block"><span className="mb-1 block text-xs font-medium text-gray-600">Resource types (comma-separated ARM types)</span>
        <input value={(draft.resource_types ?? []).join(", ")} onChange={(e) => set({ resource_types: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} placeholder="microsoft.storage/storageaccounts" className={inputCls} /></label>
      <label className="mt-2 block"><span className="mb-1 block text-xs font-medium text-gray-600">KQL (violation predicate; begins with | where … | project id, name, type, resourceGroup, subscriptionId)</span>
        <textarea value={draft.kql ?? ""} onChange={(e) => set({ kql: e.target.value })} rows={3} className={`${inputCls} font-mono text-xs`} /></label>
      <label className="mt-2 block"><span className="mb-1 block text-xs font-medium text-gray-600">Remediation</span>
        <input value={draft.remediation ?? ""} onChange={(e) => set({ remediation: e.target.value })} className={inputCls} /></label>
      <label className="mt-2 flex items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={draft.enabled ?? true} onChange={(e) => set({ enabled: e.target.checked })} />Enabled</label>
      <div className="mt-3 flex gap-2">
        <button onClick={onSave} disabled={!draft.title || !draft.kql?.trim()} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">Save control</button>
        <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
      </div>
    </div>
  );
}

// ---------------- Panel ----------------
type Tab = "run" | "portfolio" | "custom" | "trash";
export function AssessmentsPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const [tab, setTab] = useState<Tab>("run");
  // Refresh the admin-tunable score color bands (Settings → Assessments & Architecture).
  const catalogQ = useQuery({ queryKey: ["assessmentCatalog"], queryFn: api.assessmentCatalog, staleTime: 60_000 });
  const bands = catalogQ.data?.score_bands;
  if (bands) {
    SCORE_GOOD = bands.good;
    SCORE_WARN = bands.warn;
  }
  const runsQ = useQuery({
    queryKey: ["assessmentRuns"],
    queryFn: () => api.assessmentRuns(),
    // Poll while any run is queued/running so background progress shows live in history.
    refetchInterval: (q) => {
      const data = q.state.data as { runs?: AssessmentRunSummary[] } | undefined;
      const active = (data?.runs ?? []).some((r) => r.status === "queued" || r.status === "running");
      return active ? 2500 : false;
    },
  });
  const runs = (runsQ.data?.runs ?? []) as AssessmentRunSummary[];
  const [error, setError] = useState("");

  async function del(runId: string) {
    if (!window.confirm("Move this assessment run to the trash?")) return;
    try { await api.deleteAssessmentRun(runId); qc.invalidateQueries({ queryKey: ["assessmentRuns"] }); qc.invalidateQueries({ queryKey: ["assessmentPortfolio"] }); }
    catch (e) { setError(formatError(e)); }
  }
  async function cancel(runId: string) {
    try { await api.cancelAssessmentRun(runId); qc.invalidateQueries({ queryKey: ["assessmentRuns"] }); }
    catch (e) { setError(formatError(e)); }
  }

  if (id) {
    return <div className="h-full overflow-y-auto bg-gray-50"><RunDetail key={id} runId={id} onBack={() => navigate("/assessments")} /></div>;
  }

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="space-y-5 p-6">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Assessments</h1>
          <p className="mt-1 text-sm text-gray-500">Evaluate workloads against the Azure Well-Architected pillars (Security, Reliability, Cost, Operations, Performance), mapped to CIS / NIST / ISO 27001. Score 0–100, track trends, accept risks, assign owners, open tickets, and schedule recurring runs.</p>
        </div>
        {error && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
        <div className="flex gap-1 border-b">
          {([["run", "Run & history"], ["portfolio", "Portfolio"], ["custom", "Custom controls"], ["trash", "Trash"]] as [Tab, string][]).map(([t, label]) => (
            <button key={t} onClick={() => setTab(t)} className={`px-3 py-1.5 text-sm ${tab === t ? "border-b-2 border-brand font-medium text-brand" : "text-gray-500 hover:text-gray-700"}`}>{label}</button>
          ))}
        </div>
        {tab === "run" && (
          <>
            <RunFlow onQueued={() => qc.invalidateQueries({ queryKey: ["assessmentRuns"] })} />
            <div><h2 className="mb-2 font-medium text-gray-800">History</h2><RunHistory runs={runs} onOpen={(rid) => navigate(`/assessments/${rid}`)} onDelete={del} onCancel={cancel} /></div>
          </>
        )}
        {tab === "portfolio" && <PortfolioView onOpen={(rid) => navigate(`/assessments/${rid}`)} />}
        {tab === "custom" && <CustomChecksView />}
        {tab === "trash" && <TrashView onOpen={(rid) => navigate(`/assessments/${rid}`)} />}
      </div>
    </div>
  );
}

function TrashView({ onOpen }: { onOpen: (id: string) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["assessmentTrash"], queryFn: api.assessmentTrash });
  const runs = (q.data?.runs ?? []) as AssessmentRunSummary[];
  const [error, setError] = useState("");

  const invalidate = () => { qc.invalidateQueries({ queryKey: ["assessmentTrash"] }); qc.invalidateQueries({ queryKey: ["assessmentRuns"] }); };
  async function restore(id: string) { try { await api.restoreAssessmentRun(id); invalidate(); } catch (e) { setError(formatError(e)); } }
  async function purge(id: string) { if (!window.confirm("Permanently delete this run? This cannot be undone.")) return; try { await api.purgeAssessmentRun(id); invalidate(); } catch (e) { setError(formatError(e)); } }
  async function empty() { if (!window.confirm("Permanently delete ALL runs in the trash? This cannot be undone.")) return; try { await api.emptyAssessmentTrash(); invalidate(); } catch (e) { setError(formatError(e)); } }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">Deleted assessment runs are kept here until permanently removed. Restore brings a run back into history.</p>
        {runs.length > 0 && <button onClick={() => void empty()} className="rounded-lg border border-red-200 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50">Empty trash ({runs.length})</button>}
      </div>
      {error && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {runs.length === 0 ? (
        <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-8 text-center text-sm text-gray-500">Trash is empty.</div>
      ) : (
        <div className="overflow-hidden rounded-lg border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500"><tr className="border-b">
              <th className="py-2 pl-4 font-medium">Workload</th><th className="py-2 pr-3 font-medium">Status</th><th className="py-2 pr-3 font-medium">Score</th><th className="py-2 pr-3 font-medium">Pillars</th>
              <th className="py-2 pr-3 font-medium">Deleted</th><th className="py-2 pr-3" />
            </tr></thead>
            <tbody>
              {runs.map((r) => {
                const clickable = r.status === "succeeded" || r.status === "failed";
                return (
                  <tr key={r.id} className={`border-b last:border-0 ${clickable ? "cursor-pointer hover:bg-gray-50" : ""}`} onClick={() => clickable && onOpen(r.id)}>
                    <td className="py-2 pl-4 font-medium text-gray-800">{r.workload_name}</td>
                    <td className="py-2 pr-3"><RunStatusBadge status={r.status} /></td>
                    <td className={`py-2 pr-3 font-semibold ${scoreColor(r.overall_score)}`}>{r.overall_score ?? "—"}</td>
                    <td className="py-2 pr-3 text-gray-500">{r.pillars.map((p) => PILLAR_META[p]?.icon ?? "").join(" ")}</td>
                    <td className="py-2 pr-3 text-gray-400">{formatTimestamp(r.deleted_at ?? undefined)}</td>
                    <td className="py-2 pr-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button onClick={(e) => { e.stopPropagation(); void restore(r.id); }} className="rounded border border-green-200 px-2 py-0.5 text-xs text-green-700 hover:bg-green-50">Restore</button>
                        <button onClick={(e) => { e.stopPropagation(); void purge(r.id); }} className="rounded border border-red-200 px-2 py-0.5 text-xs text-red-600 hover:bg-red-50">Delete forever</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

