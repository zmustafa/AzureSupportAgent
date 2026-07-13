import { useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  downloadBlob,
  type AlertsManagerCapabilities,
  type AlertsManagerChange,
  type AlertsManagerChangeDetails,
  type AlertAnalysisActionGroup,
  type AlertAnalysisGap,
  type AlertAnalysisOverlap,
  type AlertAnalysisRule,
  type AlertAnalysisRuleCost,
  type AlertAnalysisRefreshJobResponse,
  type AlertAnalysisSnapshot,
  type EditableActionGroup,
  type FiredAlertInstance,
  type ManagedActionGroup,
  type EditableAlertRule,
  type GapDeploymentPlanStatus,
  type ManagedAlertRule,
} from "../api";
import { ensureUtc, formatError } from "../utils/format";
import { queryKeys } from "../queryKeys";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { ManagementGroupPicker } from "./ManagementGroupPicker";
import { TrendChart } from "./TrendChart";
import { Skeleton } from "../utils/perf";
import { AlertRuleEditor, ManagedAlertRulesTable, newAlertRule, ruleFromGap, type RuleFamily } from "./AlertRuleManagement";
import { NotificationSimulatorPanel } from "./AlertPhase5Management";
import { AdvancedActionGroupReceivers } from "./AdvancedActionGroupReceivers";
import { AzurePlacementFields } from "./AlertsAuthoringSelectors";
import { AlertBlueprintPlanner } from "./AlertBlueprintPlanner";
import { GapRemediationPlanner } from "./GapRemediationPlanner";
import { ActivityLogCoverageSection, ActivityLogSetupWizard } from "./ActivityLogCoverage";
import { ActivityLogDiagnosticsWizard } from "./ActivityLogDiagnosticsWizard";

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-50 text-emerald-700",
  overlap: "bg-amber-50 text-amber-700",
  gap: "bg-rose-50 text-rose-700",
  accepted: "bg-indigo-50 text-indigo-700",
};
const RISK_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  error: "bg-orange-100 text-orange-700",
  warning: "bg-amber-100 text-amber-700",
  informational: "bg-sky-100 text-sky-700",
};
const COST_CONFIDENCE_STYLE: Record<AlertAnalysisRuleCost["confidence"], string> = {
  high: "bg-emerald-50 text-emerald-700",
  medium: "bg-amber-50 text-amber-700",
  low: "bg-rose-50 text-rose-700",
  none: "bg-gray-100 text-gray-600",
};
const COST_FAMILY_LABELS: Record<string, string> = {
  metric: "Metric",
  log: "Log query",
  activity_log: "Activity log",
  smart_detector: "Smart detector",
  prometheus: "Prometheus",
  unknown: "Unknown",
};

type Tab = "overview" | "inbox" | "overlaps" | "gaps" | "rules" | "manage-rules" | "action-groups" | "deployment-plans" | "visualize" | "changes";
const PAGE_SIZE = 100;
const CHANGES_PAGE_SIZE = 100;
type ChangesView = "all" | "action_required" | "archived";
type ChangesSort = "newest" | "oldest" | "status" | "risk" | "change";
const VALID_TABS = new Set<Tab>(["overview", "inbox", "overlaps", "gaps", "rules", "manage-rules", "action-groups", "deployment-plans", "visualize", "changes"]);
type ChangesPage = { changes: AlertsManagerChange[]; total: number; page: number; page_size: number; pending_count: number; approved_count: number; actionable_count: number };

function PagedView<T>({ rows, page, onPage, children }: { rows: T[]; page: number; onPage: (page: number) => void; children: (rows: T[]) => ReactNode }) {
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(Math.max(1, page), pageCount);
  const start = (current - 1) * PAGE_SIZE;
  return <div className="space-y-2">
    {children(rows.slice(start, start + PAGE_SIZE))}
    {rows.length > PAGE_SIZE && <div className="flex items-center justify-between rounded-lg border bg-white px-3 py-2 text-xs text-gray-600">
      <span>Showing {start + 1}–{Math.min(start + PAGE_SIZE, rows.length)} of {rows.length} · 100 per page</span>
      <div className="flex items-center gap-2">
        <button disabled={current <= 1} onClick={() => onPage(current - 1)} className="rounded border px-2.5 py-1 disabled:opacity-40">Previous</button>
        <span>Page {current} of {pageCount}</span>
        <button disabled={current >= pageCount} onClick={() => onPage(current + 1)} className="rounded border px-2.5 py-1 disabled:opacity-40">Next</button>
      </div>
    </div>}
  </div>;
}

function PageBar({ total, page, pageSize, onPage }: { total: number; page: number; pageSize: number; onPage: (page: number) => void }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, page), pageCount);
  const start = (current - 1) * pageSize;
  if (total <= pageSize) return null;
  return <div className="flex items-center justify-between rounded-lg border bg-white px-3 py-2 text-xs text-gray-600">
    <span>Showing {start + 1}–{Math.min(start + pageSize, total)} of {total} · {pageSize} per page</span>
    <div className="flex items-center gap-2"><button disabled={current <= 1} onClick={() => onPage(current - 1)} className="rounded border px-2.5 py-1 disabled:opacity-40">Previous</button><span>Page {current} of {pageCount}</span><button disabled={current >= pageCount} onClick={() => onPage(current + 1)} className="rounded border px-2.5 py-1 disabled:opacity-40">Next</button></div>
  </div>;
}

function ageText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function elapsedText(startedAt?: string, finishedAt?: string | null): string {
  if (!startedAt) return "0s";
  const end = finishedAt ? Date.parse(finishedAt) : Date.now();
  const seconds = Math.max(0, Math.floor((end - Date.parse(startedAt)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function AnalysisProgress({ state, compact = false }: { state?: AlertAnalysisRefreshJobResponse; compact?: boolean }) {
  const [, tick] = useState(0);
  const [dismissedJobId, setDismissedJobId] = useState("");
  const job = state?.job;
  useEffect(() => {
    if (job?.status !== "running") return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [job?.status]);
  useEffect(() => {
    if (!job || job.status !== "done") return;
    const timer = window.setTimeout(() => setDismissedJobId(job.id), 2000);
    return () => window.clearTimeout(timer);
  }, [job?.id, job?.status]);
  if (!job || dismissedJobId === job.id) return null;
  const lines = state?.progress ?? [];
  const current = lines.at(-1)?.message || job.last_message || "Preparing analysis…";
  const phase = lines.at(-1)?.phase || (job.status === "running" ? "start" : job.status);
  const running = job.status === "running";
  return <section role="status" aria-live="polite" className={`${compact ? "mt-3" : "mt-5 text-left"} overflow-hidden rounded-xl border ${job.status === "error" ? "border-red-200 bg-red-50" : "border-sky-200 bg-sky-50/70"}`}>
    <div className="flex items-start gap-3 px-4 py-3">
      <span className={`mt-1 h-2.5 w-2.5 flex-none rounded-full ${running ? "animate-pulse bg-sky-500" : job.status === "done" ? "bg-emerald-500" : "bg-red-500"}`} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2"><span className="text-xs font-semibold capitalize text-gray-900">{phase.replaceAll("_", " ")}</span><span className="text-[10px] tabular-nums text-gray-500">Elapsed {elapsedText(job.started_at, job.finished_at)}</span>{running && <span className="rounded bg-sky-100 px-2 py-0.5 text-[10px] font-medium text-sky-700">Running on server</span>}</div>
        <p className="mt-0.5 text-xs text-gray-700">{current}</p>
        <p className="mt-1 text-[10px] text-gray-500">This analysis continues in the background if you navigate away or close this page. Return to reconnect to its progress.</p>
      </div>
    </div>
    {running && <div className="h-1 overflow-hidden bg-sky-100"><div className="h-full w-1/3 animate-pulse rounded-full bg-sky-500" /></div>}
    {!compact && lines.length > 0 && <ol className="max-h-64 space-y-1 overflow-auto border-t border-sky-100 bg-white/70 px-4 py-3">
      {lines.map((line) => <li key={line.seq} className="flex gap-2 text-[11px] leading-5 text-gray-600"><span className={`${line.level === "error" ? "text-red-500" : line.phase === "done" ? "text-emerald-500" : "text-sky-500"}`}>{line.level === "error" ? "!" : "✓"}</span><span>{line.message}</span></li>)}
    </ol>}
  </section>;
}

function Kpi({ label, value, tone = "gray", hint }: { label: string; value: number; tone?: string; hint?: string }) {
  const colors: Record<string, string> = {
    gray: "border-gray-200 bg-white text-gray-900",
    red: "border-red-200 bg-red-50 text-red-700",
    amber: "border-amber-200 bg-amber-50 text-amber-700",
    blue: "border-blue-200 bg-blue-50 text-blue-700",
    green: "border-emerald-200 bg-emerald-50 text-emerald-700",
  };
  return (
    <div className={`flex h-11 min-w-0 flex-col justify-center rounded-lg border px-2.5 py-1 ${colors[tone] ?? colors.gray}`} title={hint ?? label}>
      <div className="text-base font-semibold leading-5 tabular-nums">{value}</div>
      <div className="truncate whitespace-nowrap text-[9px] font-medium uppercase leading-3 tracking-wide opacity-70" title={label}>{label}</div>
    </div>
  );
}

function PortalLink({ id }: { id: string }) {
  if (!id?.startsWith("/")) return null;
  return (
    <a
      href={`https://portal.azure.com/#@/resource${id}/overview`}
      target="_blank"
      rel="noreferrer"
      title="Open in Azure Portal"
      className="text-xs text-brand hover:underline"
    >
      Portal ↗
    </a>
  );
}

function formatCost(value: number | null | undefined, currency = "USD"): string {
  if (value == null) return "Unbounded";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${currency} ${value.toFixed(2)}`;
  }
}

function costRange(minimum: number | null | undefined, maximum: number | null | undefined, currency: string): string {
  if (minimum == null && maximum == null) return "Range unavailable";
  if (maximum == null) return `${formatCost(minimum, currency)}+`;
  if (minimum === maximum) return formatCost(minimum, currency);
  return `${formatCost(minimum, currency)}–${formatCost(maximum, currency)}`;
}

function costStatusLabel(cost?: Pick<AlertAnalysisRuleCost, "status" | "monthly_usd">): string {
  if (!cost || cost.status === "unknown" || cost.monthly_usd == null) return "Unpriced";
  if (cost.status === "direct_free") return "Free";
  if (cost.status === "range_estimate") return "Range estimate";
  if (cost.status === "partial_estimate") return "Lower bound";
  return "Estimated";
}

function Overview({ overlaps, gaps, rules, costSummary, activityLogCoverage }: { overlaps: AlertAnalysisOverlap[]; gaps: AlertAnalysisGap[]; rules: AlertAnalysisRule[]; costSummary?: AlertAnalysisSnapshot["cost_summary"]; activityLogCoverage: ReactNode }) {
  const top = [
    ...overlaps.map((item) => ({
      key: item.id,
      kind: item.type === "exact" ? "Exact overlap" : item.type === "notification" ? "Notification fan-out" : "Near overlap",
      title: item.rule_names.join(" ↔ "),
      detail: `${item.signal_name} · ${item.notification_overlap ? `${item.shared_recipient_count} shared recipient path(s)` : "different recipients"}`,
      risk: item.confidence === "high" ? "error" : "warning",
    })),
    ...gaps.slice(0, 15).map((item, index) => ({
      key: `gap-${index}`,
      kind: item.type.replaceAll("_", " "),
      title: item.rule_name || item.resource_name || item.signal,
      detail: item.recommendation,
      risk: item.risk,
    })),
  ].slice(0, 12);
  const clean = rules.filter((rule) => rule.finding_status === "ok").length;
  const currency = costSummary?.currency ?? "USD";
  const confidenceCounts = rules.reduce<Record<string, number>>((counts, rule) => {
    const confidence = rule.cost?.confidence ?? "none";
    counts[confidence] = (counts[confidence] ?? 0) + 1;
    return counts;
  }, {});
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-xl border bg-white p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">Rationalization opportunity</div>
          <div className="mt-2 text-3xl font-semibold text-amber-600">{overlaps.length}</div>
          <p className="mt-1 text-xs text-gray-500">Rule groups that may evaluate the same symptom.</p>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">Coverage & routing gaps</div>
          <div className="mt-2 text-3xl font-semibold text-red-600">{gaps.length}</div>
          <p className="mt-1 text-xs text-gray-500">Missing baselines, disabled rules, or ineffective notification paths.</p>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-gray-400">Clean rules</div>
          <div className="mt-2 text-3xl font-semibold text-emerald-600">{clean}</div>
          <p className="mt-1 text-xs text-gray-500">No overlap or routing issue detected in this snapshot.</p>
        </div>
        <div className="rounded-xl border border-sky-200 bg-gradient-to-br from-sky-50 to-white p-4">
          <div className="text-xs font-medium uppercase tracking-wide text-sky-600">Estimated monthly total</div>
          <div className="mt-2 text-3xl font-semibold tabular-nums text-sky-700">{costSummary ? formatCost(costSummary.current.monthly_usd, currency) : "—"}</div>
          <p className="mt-1 text-xs text-gray-500">{costSummary ? `${costRange(costSummary.current.monthly_min_usd, costSummary.current.monthly_max_usd, currency)} estimated range` : "Run a new analysis to calculate rule costs."}</p>
        </div>
      </div>
      {costSummary && <section className="overflow-hidden rounded-xl border bg-white">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3">
          <div><h2 className="text-sm font-semibold text-gray-900">Alert rule cost estimate</h2><p className="text-xs text-gray-500">Reference alert-rule charges only; this is not an Azure billing quote.</p></div>
          <div className="text-right text-[10px] text-gray-500"><div className="font-medium text-gray-700">{currency} · per {costSummary.period}</div><div>Catalog {costSummary.catalog_effective_date || "date unavailable"}</div></div>
        </div>
        <div className="grid gap-3 border-b bg-gray-50/60 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-lg border bg-white p-3"><div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Current enabled spend</div><div className="mt-1 text-lg font-semibold tabular-nums text-gray-900">{formatCost(costSummary.current.monthly_usd, currency)}</div><div className="text-[10px] text-gray-500">{costRange(costSummary.current.monthly_min_usd, costSummary.current.monthly_max_usd, currency)}</div></div>
          <div className="rounded-lg border bg-white p-3"><div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Potential disabled spend</div><div className="mt-1 text-lg font-semibold tabular-nums text-indigo-700">{formatCost(costSummary.potential_disabled_monthly, currency)}</div><div className="text-[10px] text-gray-500">If disabled rules were enabled · {costRange(costSummary.potential_disabled_monthly_min, costSummary.potential_disabled_monthly_max, currency)}</div></div>
          <div className="rounded-lg border bg-white p-3"><div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Estimate coverage</div><div className="mt-1 text-lg font-semibold tabular-nums text-gray-900">{costSummary.priced_count} priced</div><div className={`text-[10px] ${costSummary.unknown_count ? "text-rose-600" : "text-emerald-600"}`}>{costSummary.unknown_count} unknown / unpriced</div></div>
          <div className="rounded-lg border bg-white p-3"><div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Confidence</div><div className="mt-2 flex flex-wrap gap-1">{(["high", "medium", "low", "none"] as const).filter((level) => confidenceCounts[level]).map((level) => <span key={level} className={`rounded px-2 py-0.5 text-[10px] font-medium ${COST_CONFIDENCE_STYLE[level]}`}>{confidenceCounts[level]} {level}</span>)}</div><div className="mt-1 text-[10px] text-gray-500">Varies by rule and observable cardinality.</div></div>
        </div>
        <div className="grid divide-y lg:grid-cols-2 lg:divide-x lg:divide-y-0">
          <div className="p-4"><h3 className="text-xs font-semibold text-gray-800">Family breakdown</h3><div className="mt-2 space-y-2">{Object.entries(costSummary.by_family).map(([family, item]) => <div key={family} className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 text-xs"><div className="min-w-0"><span className="font-medium text-gray-700">{COST_FAMILY_LABELS[family] ?? family.replaceAll("_", " ")}</span><span className="ml-1 text-[10px] text-gray-400">{item.rule_count} rule{item.rule_count === 1 ? "" : "s"}{item.unknown_count ? ` · ${item.unknown_count} unknown` : ""}</span></div><div className="text-right"><div className="tabular-nums text-gray-700">{formatCost(item.current.monthly_usd, currency)}</div><div className="text-[9px] text-gray-400">enabled</div></div><div className="min-w-[72px] text-right"><div className="tabular-nums text-indigo-600">{formatCost(item.disabled.monthly_usd, currency)}</div><div className="text-[9px] text-gray-400">disabled</div></div></div>)}</div></div>
          <div className="p-4"><h3 className="text-xs font-semibold text-gray-800">Top-cost rules</h3>{costSummary.top_rules.length ? <div className="mt-2 space-y-2">{costSummary.top_rules.slice(0, 6).map((rule, index) => <div key={rule.rule_id || `${rule.rule_name}-${index}`} className="flex items-center gap-2 text-xs"><span className="w-4 text-[10px] text-gray-400">{index + 1}</span><div className="min-w-0 flex-1"><div className="truncate font-medium text-gray-700" title={rule.rule_name}>{rule.rule_name || "Unnamed rule"}</div><div className="text-[9px] capitalize text-gray-400">{COST_FAMILY_LABELS[rule.family] ?? rule.family} · {costStatusLabel(rule)} · {rule.confidence} confidence{rule.enabled ? "" : " · disabled"}</div></div><span className="whitespace-nowrap font-medium tabular-nums text-gray-800">{formatCost(rule.monthly_usd, currency)}</span></div>)}</div> : <div className="mt-4 text-xs text-gray-400">No priced rules in this snapshot.</div>}</div>
        </div>
        <details className="border-t px-4 py-3 text-xs"><summary className="cursor-pointer font-medium text-gray-700">Pricing status, scope, and assumptions</summary><div className="mt-2 max-w-4xl space-y-1 text-[11px] leading-5 text-gray-500"><p>{costSummary.catalog_scope}</p>{costSummary.assumptions.map((assumption) => <p key={assumption}>• {assumption}</p>)}<a href={costSummary.catalog_source} target="_blank" rel="noreferrer" className="inline-block text-brand hover:underline">Pricing catalog source ↗</a><div className="font-mono text-[9px] text-gray-400">{costSummary.catalog_version}</div></div></details>
      </section>}
      {activityLogCoverage}
      <section className="overflow-hidden rounded-xl border bg-white">
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-900">Priority review queue</h2>
          <p className="text-xs text-gray-500">Highest-confidence overlaps and actionable gaps first.</p>
        </div>
        {top.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-400">No rationalization findings in this snapshot.</div>
        ) : (
          <div className="divide-y">
            {top.map((item) => (
              <div key={item.key} className="flex items-start gap-3 px-4 py-3">
                <span className={`mt-0.5 rounded px-2 py-0.5 text-[10px] font-medium ${RISK_STYLE[item.risk] ?? RISK_STYLE.warning}`}>{item.kind}</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-gray-800">{item.title || "Unnamed finding"}</div>
                  <div className="mt-0.5 text-xs text-gray-500">{item.detail}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function OverlapsTable({ rows, onDismiss }: { rows: AlertAnalysisOverlap[]; onDismiss: (id: string) => void }) {
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full min-w-[900px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500">
          <tr><th className="px-3 py-2">Confidence</th><th className="px-3 py-2">Signal / target</th><th className="px-3 py-2">Overlapping rules</th><th className="px-3 py-2">Notification impact</th><th className="px-3 py-2">Recommendation</th><th className="px-3 py-2" /></tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((row) => (
            <tr key={row.id} className="align-top hover:bg-gray-50">
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 font-medium ${row.confidence === "high" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>{row.confidence} · {row.type}</span></td>
              <td className="max-w-xs px-3 py-3"><div className="font-medium text-gray-800">{row.signal_name}</div><div className="mt-1 break-all text-[10px] text-gray-400">{row.target_id}</div></td>
              <td className="px-3 py-3"><div className="space-y-1">{row.rule_names.map((name) => <div key={name} className="rounded bg-gray-50 px-2 py-1 text-gray-700">{name}</div>)}</div></td>
              <td className="px-3 py-3">{row.notification_overlap ? <span className="font-medium text-red-600">{row.shared_recipient_count} shared destination{row.shared_recipient_count === 1 ? "" : "s"}</span> : <span className="text-gray-400">No shared destination</span>}</td>
              <td className="max-w-sm px-3 py-3 text-gray-600">{row.recommendation}</td>
              <td className="px-3 py-3"><button onClick={() => onDismiss(row.id)} className="whitespace-nowrap rounded border px-2 py-1 text-[10px] text-indigo-600 hover:bg-indigo-50">Accept overlap</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No overlapping rule groups detected.</div>}
    </div>
  );
}

function gapIdentity(row: AlertAnalysisGap, _index = 0): string {
  const target = row.rule_id || row.resource_id || row.action_group_id || row.resource_name || "unknown-target";
  const signal = row.alert_key || row.signal || row.rule_name || "unknown-signal";
  return [row.decision_key || row.type, target, signal].map((value) => String(value).trim().toLowerCase()).join("|");
}

function isActiveGapPlanStatus(status?: GapDeploymentPlanStatus["status"]): boolean {
  return status === "pending" || status === "approved";
}

function GapsTable({ rows, selectedRows, selectedIds, plansByGap, canPlan, onSelectionChange, onCreatePlan, onOpenPlan, onExempt, onCreateRule }: {
  rows: AlertAnalysisGap[];
  selectedRows: AlertAnalysisGap[];
  selectedIds: Set<string>;
  plansByGap: Record<string, GapDeploymentPlanStatus>;
  canPlan: boolean;
  onSelectionChange: (ids: Set<string>) => void;
  onCreatePlan: () => void;
  onOpenPlan: (planId: string) => void;
  onExempt: (row: AlertAnalysisGap, index: number) => void;
  onCreateRule?: (row: AlertAnalysisGap) => void;
}) {
  const selectable = rows.filter((row, index) => {
    const active = plansByGap[gapIdentity(row, index)];
    return (row.type === "baseline_missing" || row.type === "baseline_misconfigured") && !!row.resource_id && !!row.signal && !isActiveGapPlanStatus(active?.status);
  });
  const visibleIds = selectable.map((row) => gapIdentity(row));
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
  const resourceCount = new Set(selectedRows.map((row) => row.resource_id).filter(Boolean)).size;
  const severitySummary = Object.entries(selectedRows.reduce<Record<string, number>>((counts, row) => ({ ...counts, [row.risk]: (counts[row.risk] ?? 0) + 1 }), {})).map(([risk, count]) => `${count} ${risk}`).join(" · ");
  function toggleAllVisible() {
    const next = new Set(selectedIds);
    if (allVisibleSelected) visibleIds.forEach((id) => next.delete(id));
    else visibleIds.forEach((id) => next.add(id));
    onSelectionChange(next);
  }
  return (
    <div className="overflow-hidden rounded-xl border bg-white">
      {selectedIds.size > 0 && <div className="flex flex-wrap items-center gap-3 border-b border-indigo-200 bg-indigo-50 px-4 py-3" data-testid="gap-selection-bar"><div><div className="text-xs font-semibold text-indigo-900">{selectedIds.size} gap{selectedIds.size === 1 ? "" : "s"} selected</div><div className="text-[10px] text-indigo-700">{resourceCount} resource{resourceCount === 1 ? "" : "s"}{severitySummary ? ` · ${severitySummary}` : ""}</div></div><div className="ml-auto flex gap-2"><button onClick={() => onSelectionChange(new Set())} className="rounded border border-indigo-200 bg-white px-3 py-1.5 text-xs text-indigo-700 hover:bg-indigo-100">Clear</button><button data-action="create-gaps-plan" disabled={!canPlan} onClick={onCreatePlan} className="rounded bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-40">Create remediation plan</button></div></div>}
      <div className="overflow-auto">
      <table className="w-full min-w-[980px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2"><input type="checkbox" aria-label="Select all visible actionable gaps" checked={allVisibleSelected} disabled={!canPlan || !visibleIds.length} onChange={toggleAllVisible} /></th><th className="px-3 py-2">Risk</th><th className="px-3 py-2">Gap</th><th className="px-3 py-2">Resource / rule</th><th className="px-3 py-2">Signal</th><th className="px-3 py-2">Recommended action</th><th className="px-3 py-2" /></tr></thead>
        <tbody className="divide-y">
          {rows.map((row, index) => {
            const id = gapIdentity(row, index);
            const plan = plansByGap[id];
            const activePlan = isActiveGapPlanStatus(plan?.status);
            const actionable = (row.type === "baseline_missing" || row.type === "baseline_misconfigured") && !!row.resource_id && !!row.signal && !activePlan;
            return (
            <tr key={id} data-testid={`gap-row-${id}`} className={`align-top hover:bg-gray-50 ${selectedIds.has(id) ? "bg-indigo-50/60" : ""}`}>
              <td className="px-3 py-3"><input type="checkbox" aria-label={`Select ${row.signal || row.resource_name || "gap"}`} checked={selectedIds.has(id)} disabled={!canPlan || !actionable} title={!actionable ? activePlan ? "This gap already has an active deployment plan." : "Bulk remediation currently supports metric baseline gaps first." : undefined} onChange={(event) => { const next = new Set(selectedIds); if (event.target.checked) next.add(id); else next.delete(id); onSelectionChange(next); }} /></td>
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 font-medium ${RISK_STYLE[row.risk] ?? RISK_STYLE.warning}`}>{row.risk}</span></td>
              <td className="px-3 py-3 font-medium capitalize text-gray-700"><div>{row.type.replaceAll("_", " ")}</div>{plan && plan.status !== "none" && <button onClick={() => onOpenPlan(plan.plan_id)} className={`mt-1 rounded px-2 py-0.5 text-[10px] ${plan.status === "rejected" || plan.status === "failed" || plan.status === "stale" ? "bg-rose-50 text-rose-700" : plan.status === "applied" || plan.status === "approved" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{plan.status} plan ↗</button>}</td>
              <td className="max-w-xs px-3 py-3"><div className="font-medium text-gray-800">{row.rule_name || row.resource_name || "—"}</div><div className="mt-0.5 break-all text-[10px] text-gray-400">{row.rule_id || row.resource_id}</div></td>
              <td className="px-3 py-3 text-gray-600">{row.signal || "—"}</td>
              <td className="max-w-sm px-3 py-3 text-gray-600">{row.recommendation}</td>
              <td className="px-3 py-3"><div className="flex items-center gap-2"><PortalLink id={row.rule_id || row.resource_id} />{onCreateRule && row.resource_id && row.signal && <button disabled={activePlan} title={activePlan ? "An active deployment plan already covers this gap." : "Create a separate reviewed alert-rule change"} onClick={() => onCreateRule(row)} className="whitespace-nowrap rounded border px-2 py-1 text-[10px] text-green-700 hover:bg-green-50 disabled:cursor-not-allowed disabled:opacity-40">Create reviewed rule</button>}<button onClick={() => onExempt(row, index)} className="whitespace-nowrap rounded border px-2 py-1 text-[10px] text-indigo-600 hover:bg-indigo-50">Exempt</button></div></td>
            </tr>
          );})}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No coverage or routing gaps detected.</div>}
      </div>
    </div>
  );
}

function RulesTable({ rows, onDecision }: { rows: AlertAnalysisRule[]; onDecision: (rule: AlertAnalysisRule, action: "keep_rule" | "exempt_rule") => void }) {
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full min-w-[1250px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Status</th><th className="px-3 py-2">Rule</th><th className="px-3 py-2">Condition</th><th className="px-3 py-2">Targets</th><th className="px-3 py-2">Action groups</th><th className="px-3 py-2">Estimated cost</th><th className="px-3 py-2">Firings</th><th className="px-3 py-2">Actions</th></tr></thead>
        <tbody className="divide-y">
          {rows.map((rule) => {
            const condition = rule.conditions[0];
            return (
              <tr key={rule.id} className="align-top hover:bg-gray-50">
                <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 font-medium ${STATUS_STYLE[rule.finding_status]}`}>{rule.finding_status}</span>{!rule.enabled && <div className="mt-1 text-[10px] text-gray-400">disabled</div>}</td>
                <td className="max-w-xs px-3 py-3"><div className="font-medium text-gray-800">{rule.name}</div><div className="mt-0.5 text-[10px] text-gray-400">{rule.type.split("/").pop()} · sev {rule.severity}</div>{rule.issues.length > 0 && <div className="mt-1 text-[10px] text-red-500">{rule.issues.join(" · ")}</div>}</td>
                <td className="px-3 py-3"><div className="font-medium text-gray-700">{condition?.signal_name || "—"}</div><div className="mt-0.5 text-gray-400">{condition ? `${condition.aggregation} ${condition.operator} ${condition.threshold ?? "dynamic"}` : ""}</div><div className="text-[10px] text-gray-400">{condition?.window} / {condition?.frequency}</div></td>
                <td className="px-3 py-3"><div className="font-medium text-gray-700">{rule.effective_target_count}</div><div className="max-w-[180px] truncate text-[10px] text-gray-400" title={rule.scopes.join("\n")}>{rule.scopes[0] || "No declared scope"}</div></td>
                <td className="px-3 py-3 text-gray-600">{rule.action_group_names.join(", ") || "—"}</td>
                <td className="min-w-[145px] px-3 py-3">
                  {rule.cost?.status === "direct_free" ? <div className="font-medium text-emerald-700">Free</div> : rule.cost?.monthly_usd == null ? <div className="font-medium text-gray-500">Unpriced</div> : <><div className="font-medium tabular-nums text-gray-800">{formatCost(rule.cost.monthly_usd, rule.cost.currency)}<span className="text-[9px] font-normal text-gray-400"> / mo</span></div>{(rule.cost.status === "range_estimate" || rule.cost.status === "partial_estimate" || rule.cost.monthly_min_usd !== rule.cost.monthly_max_usd) && <div className="mt-0.5 text-[9px] tabular-nums text-gray-500">{costRange(rule.cost.monthly_min_usd, rule.cost.monthly_max_usd, rule.cost.currency)} {rule.cost.status === "partial_estimate" ? "lower bound" : "range"}</div>}</>}
                  <div className="mt-1 flex flex-wrap items-center gap-1"><span className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${rule.cost?.status === "direct_free" ? "bg-emerald-50 text-emerald-700" : rule.cost?.status === "unknown" || !rule.cost ? "bg-gray-100 text-gray-600" : "bg-sky-50 text-sky-700"}`}>{costStatusLabel(rule.cost)}</span>{rule.cost && <span title={rule.cost.assumptions.join("\n\n") || "No additional assumptions."} className={`cursor-help rounded px-1.5 py-0.5 text-[9px] font-medium ${COST_CONFIDENCE_STYLE[rule.cost.confidence]}`}>{rule.cost.confidence} confidence ⓘ</span>}</div>
                </td>
                <td className="px-3 py-3 tabular-nums text-gray-600"><div>{rule.firing_7d ?? 0} / 7d</div><div className="text-[10px] text-gray-400">{rule.firing_30d ?? 0} / 30d</div>{rule.last_fired && <div className="text-[10px] text-gray-400">last {new Date(rule.last_fired).toLocaleDateString()}</div>}</td>
                <td className="px-3 py-3"><div className="flex flex-wrap gap-1"><PortalLink id={rule.id} /><button onClick={() => onDecision(rule, "keep_rule")} className="rounded border px-2 py-1 text-[10px] text-indigo-600 hover:bg-indigo-50">Keep</button><button onClick={() => onDecision(rule, "exempt_rule")} className="rounded border px-2 py-1 text-[10px] text-gray-600 hover:bg-gray-50">Exempt</button></div></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No alert rules match the filters.</div>}
    </div>
  );
}

function ActionGroupsTable({ rows }: { rows: AlertAnalysisActionGroup[] }) {
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full min-w-[800px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Action group</th><th className="px-3 py-2">State</th><th className="px-3 py-2">Receivers</th><th className="px-3 py-2">Destinations</th><th className="px-3 py-2" /></tr></thead>
        <tbody className="divide-y">
          {rows.map((group) => (
            <tr key={group.id} className="align-top hover:bg-gray-50">
              <td className="px-3 py-3"><div className="font-medium text-gray-800">{group.name}</div><div className="text-[10px] text-gray-400">{group.resource_group}</div></td>
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 ${group.enabled && group.active_receiver_count > 0 ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{group.enabled ? "enabled" : "disabled"}</span></td>
              <td className="px-3 py-3 tabular-nums text-gray-600">{group.active_receiver_count} active / {group.receiver_count} total</td>
              <td className="px-3 py-3"><div className="flex flex-wrap gap-1">{group.receivers.map((receiver) => <span key={`${receiver.type}:${receiver.fingerprint}`} className="rounded bg-gray-100 px-2 py-0.5 text-[10px] text-gray-600" title={`Fingerprint ${receiver.fingerprint}`}>{receiver.type} · {receiver.destination || receiver.masked}</span>)}</div></td>
              <td className="px-3 py-3"><PortalLink id={group.id} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No action groups found in this scope.</div>}
    </div>
  );
}

function FiredAlertsTable({ rows, canManage, busy, onState }: { rows: FiredAlertInstance[]; canManage: boolean; busy: string; onState: (row: FiredAlertInstance, state: "New" | "Acknowledged" | "Closed") => void }) {
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full min-w-[1050px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Severity</th><th className="px-3 py-2">Alert</th><th className="px-3 py-2">State</th><th className="px-3 py-2">Service / signal</th><th className="px-3 py-2">Fired</th><th className="px-3 py-2">Target</th><th className="px-3 py-2">Actions</th></tr></thead>
        <tbody className="divide-y">
          {rows.map((row) => (
            <tr key={row.id} className="align-top hover:bg-gray-50">
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 font-medium ${row.severity === "Sev0" || row.severity === "Sev1" ? "bg-red-50 text-red-700" : row.severity === "Sev2" ? "bg-amber-50 text-amber-700" : "bg-sky-50 text-sky-700"}`}>{row.severity || "—"}</span></td>
              <td className="max-w-sm px-3 py-3"><div className="font-medium text-gray-800">{row.rule_name || row.name}</div><div className="mt-1 line-clamp-2 text-[10px] text-gray-500">{row.description || row.monitor_condition}</div></td>
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 ${row.state === "Closed" ? "bg-gray-100 text-gray-600" : row.state === "Acknowledged" ? "bg-indigo-50 text-indigo-700" : "bg-rose-50 text-rose-700"}`}>{row.state}</span></td>
              <td className="px-3 py-3 text-gray-600"><div>{row.monitor_service || "—"}</div><div className="text-[10px] text-gray-400">{row.signal_type}</div></td>
              <td className="px-3 py-3 text-gray-600">{row.fired_at ? new Date(row.fired_at).toLocaleString() : "—"}</td>
              <td className="max-w-xs px-3 py-3"><div className="truncate text-[10px] text-gray-500" title={row.target_ids.join("\n")}>{row.target_ids[0] || "—"}</div></td>
              <td className="px-3 py-3"><div className="flex flex-wrap gap-1"><PortalLink id={row.target_ids[0]} />{canManage && row.state !== "Acknowledged" && <button disabled={busy === row.id} onClick={() => onState(row, "Acknowledged")} className="rounded border px-2 py-1 text-[10px] text-indigo-700 disabled:opacity-50">Acknowledge</button>}{canManage && row.state !== "Closed" && <button disabled={busy === row.id} onClick={() => onState(row, "Closed")} className="rounded border px-2 py-1 text-[10px] text-gray-700 disabled:opacity-50">Close</button>}{canManage && row.state !== "New" && <button disabled={busy === row.id} onClick={() => onState(row, "New")} className="rounded border px-2 py-1 text-[10px] text-rose-700 disabled:opacity-50">Reopen</button>}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No fired alerts were found in the selected scope and time window.</div>}
    </div>
  );
}

function ManagedActionGroupsTable({ rows, caps, busy, refreshing, onRefresh, onCreate, onEdit, onClone, onToggle, onDelete, onTest }: { rows: ManagedActionGroup[]; caps?: AlertsManagerCapabilities; busy: string; refreshing: boolean; onRefresh: () => void; onCreate: () => void; onEdit: (row: ManagedActionGroup) => void; onClone: (row: ManagedActionGroup) => void; onToggle: (row: ManagedActionGroup) => void; onDelete: (row: ManagedActionGroup) => void; onTest: (row: ManagedActionGroup) => void }) {
  const azureRows = rows.map((group) => {
    const dependencies = group.dependencies;
    return { ...group, dependencies, dependency_count: dependencies.length };
  });
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between rounded-xl border bg-white px-4 py-3">
        <div><h2 className="text-sm font-semibold text-gray-900">Azure Action Groups</h2><p className="text-xs text-gray-500">Live Azure inventory with rule dependencies and full receiver destinations.</p></div>
        <div className="flex gap-2"><button disabled={refreshing} onClick={onRefresh} className="rounded-lg border px-3 py-1.5 text-xs text-gray-700 disabled:opacity-50">{refreshing ? "Refreshing…" : "↻ Refresh"}</button>{caps?.can_manage_action_groups && !caps.read_only && <button onClick={onCreate} className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white">+ Create action group</button>}</div>
      </div>
      {caps?.read_only && <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">The selected connection is read-only. Management controls are disabled.</div>}
      <div className="overflow-auto rounded-xl border bg-white">
        <table className="w-full min-w-[1050px] text-left text-xs">
          <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Action group</th><th className="px-3 py-2">State</th><th className="px-3 py-2">Receivers</th><th className="px-3 py-2">Dependencies</th><th className="px-3 py-2">Destinations</th><th className="px-3 py-2">Actions</th></tr></thead>
          <tbody className="divide-y">
            {azureRows.map((group) => (
              <tr key={group.id} className="align-top hover:bg-gray-50">
                <td className="px-3 py-3"><div className="font-medium text-gray-800">{group.name}</div><div className="text-[10px] text-gray-400">{group.resource_group} · {group.location} · {group.short_name}</div></td>
                <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 ${group.enabled ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-600"}`}>{group.enabled ? "enabled" : "disabled"}</span></td>
                <td className="px-3 py-3 text-gray-600">{group.active_receiver_count} active / {group.receiver_count} total</td>
                <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 ${group.dependency_count ? "bg-indigo-50 text-indigo-700" : "bg-gray-100 text-gray-500"}`}>{group.dependency_count} Azure alert rule{group.dependency_count === 1 ? "" : "s"}</span>{group.dependencies.slice(0, 3).map((dep) => <div key={`${dep.type}:${dep.id}`} className="mt-1 max-w-[220px] truncate text-[10px] text-gray-400" title={`${dep.type}: ${dep.id}`}>{dep.name}</div>)}</td>
                <td className="px-3 py-3"><div className="flex max-w-sm flex-wrap gap-1">{group.receivers.map((receiver) => <span key={`${receiver.type}:${receiver.fingerprint}`} className="rounded bg-gray-100 px-2 py-0.5 text-[10px] text-gray-600">{receiver.type} · {receiver.destination || receiver.masked}</span>)}</div></td>
                <td className="px-3 py-3"><div className="flex flex-wrap gap-1"><PortalLink id={group.id} />{caps?.can_manage_action_groups && !caps.read_only && <><button disabled={!!busy} onClick={() => onEdit(group)} className="rounded border px-2 py-1 text-[10px] text-indigo-700 disabled:opacity-50">Edit</button><button disabled={!!busy} onClick={() => onClone(group)} className="rounded border px-2 py-1 text-[10px] text-indigo-700 disabled:opacity-50">Clone</button><button disabled={!!busy} onClick={() => onToggle(group)} className="rounded border px-2 py-1 text-[10px] text-gray-700 disabled:opacity-50">{group.enabled ? "Disable" : "Enable"}</button></>}{caps?.can_test_notifications && !caps.read_only && group.receiver_count > 0 && <button disabled={!!busy} onClick={() => onTest(group)} className="rounded border px-2 py-1 text-[10px] text-amber-700 disabled:opacity-50">Test</button>}{caps?.can_delete && !caps.read_only && <button disabled={!!busy || group.dependency_count > 0} title={group.dependency_count ? "Detach this Action Group from all Azure alert rules before deleting it." : "Request deletion"} onClick={() => onDelete(group)} className="rounded border px-2 py-1 text-[10px] text-red-700 disabled:opacity-40">Delete</button>}</div></td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No Action Groups exist in this Azure scope.</div>}
      </div>
    </div>
  );
}

function ChangesTable({ rows, caps, busy, applyingIds, preparingCount, onDecision, onApply, onBulkDecision, onBulkApply, onRollback }: { rows: AlertsManagerChange[]; caps?: AlertsManagerCapabilities; busy: string; applyingIds: Set<string>; preparingCount: number; onDecision: (row: AlertsManagerChange, decision: "approved" | "rejected") => void; onApply: (row: AlertsManagerChange) => void; onBulkDecision: (rows: AlertsManagerChange[], decision: "approved" | "rejected") => Promise<void>; onBulkApply: (rows: AlertsManagerChange[]) => Promise<void>; onRollback: (row: AlertsManagerChange) => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detailsRow, setDetailsRow] = useState<AlertsManagerChange | null>(null);
  const [details, setDetails] = useState<AlertsManagerChangeDetails | null>(null);
  const [detailsError, setDetailsError] = useState("");
  const [detailsLoading, setDetailsLoading] = useState(false);
  const selectedRows = rows.filter((row) => selected.has(row.id));
  const pendingRows = selectedRows.filter((row) => row.status === "pending");
  const approvedRows = selectedRows.filter((row) => row.status === "approved");
  const selectableRows = rows.filter((row) => row.status === "pending" || row.status === "approved");
  const allSelected = selectableRows.length > 0 && selectableRows.every((row) => selected.has(row.id));
  useEffect(() => {
    const available = new Set(selectableRows.map((row) => row.id));
    setSelected((current) => {
      const next = new Set([...current].filter((id) => available.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [rows]);
  const runDecision = async (decision: "approved" | "rejected") => {
    await onBulkDecision(pendingRows, decision);
  };
  const runApply = async () => {
    await onBulkApply(approvedRows);
  };
  const showDetails = async (row: AlertsManagerChange) => {
    setDetailsRow(row); setDetails(null); setDetailsError(""); setDetailsLoading(true);
    try { setDetails(await api.alertsManagerChangeDetails(row.id)); }
    catch (cause) { setDetailsError(formatError(cause)); }
    finally { setDetailsLoading(false); }
  };
  return (
    <div className="overflow-hidden rounded-xl border bg-white">
      {busy === "bulk" && <div className="flex items-center gap-2 border-b border-sky-200 bg-sky-50 px-4 py-3 text-xs font-medium text-sky-800"><span className="h-2 w-2 animate-pulse rounded-full bg-sky-500" />Preparing {preparingCount} managed change request{preparingCount === 1 ? "" : "s"} with up to 6 concurrent Azure lookups… Approval and rejection controls will appear when validation completes.</div>}
      {selectedRows.length > 0 && <div className="flex flex-wrap items-center gap-2 border-b bg-indigo-50 px-4 py-3"><div><span className="text-sm font-semibold text-indigo-900">{selectedRows.length} selected</span><span className="ml-2 text-xs text-indigo-700">{pendingRows.length} pending · {approvedRows.length} approved{applyingIds.size ? ` · ${applyingIds.size} applying` : ""}</span></div><div className="ml-auto flex flex-wrap gap-2"><button disabled={!!busy} onClick={() => setSelected(new Set())} className="rounded border border-indigo-200 bg-white px-3 py-1.5 text-xs text-indigo-700 disabled:opacity-40">Clear</button>{caps?.can_approve && <><button disabled={!!busy || !pendingRows.length} onClick={() => void runDecision("approved")} className="rounded border border-green-300 bg-white px-3 py-1.5 text-xs text-green-700 disabled:opacity-40">Approve ({pendingRows.length})</button><button disabled={!!busy || !pendingRows.length} onClick={() => void runDecision("rejected")} className="rounded border border-red-300 bg-white px-3 py-1.5 text-xs text-red-700 disabled:opacity-40">Reject ({pendingRows.length})</button><button disabled={!!busy || !approvedRows.length} onClick={() => void runApply()} className="rounded bg-gray-900 px-3 py-1.5 text-xs text-white disabled:opacity-40">{busy === "bulk-apply" ? `Applying to Azure (${applyingIds.size}/6 active)` : `Apply to Azure (${approvedRows.length})`}</button></>}</div></div>}
      <div className="overflow-auto">
      <table className="w-full min-w-[1000px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr><th className="px-3 py-2"><input type="checkbox" aria-label="Select all actionable managed changes" checked={allSelected} disabled={!selectableRows.length} onChange={(event) => setSelected(event.target.checked ? new Set(selectableRows.map((row) => row.id)) : new Set())} /></th><th className="px-3 py-2">Status</th><th className="px-3 py-2">Change</th><th className="px-3 py-2">Risk</th><th className="px-3 py-2">Safe summary</th><th className="px-3 py-2">Requested</th><th className="px-3 py-2">Actions</th></tr></thead>
        <tbody className="divide-y">
          {rows.map((row) => (
            <tr key={row.id} className="align-top hover:bg-gray-50">
              <td className="px-3 py-3"><input type="checkbox" aria-label={`Select ${row.target_name}`} disabled={row.status !== "pending" && row.status !== "approved"} checked={selected.has(row.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(row.id); else next.delete(row.id); return next; })} /></td>
              <td className="px-3 py-3">{applyingIds.has(row.id) && row.status === "approved" ? <span className="inline-flex items-center gap-1.5 rounded bg-sky-50 px-2 py-0.5 text-sky-700"><span className="h-2 w-2 animate-pulse rounded-full bg-sky-500" />applying</span> : <span className={`rounded px-2 py-0.5 ${row.status === "applied" ? "bg-green-50 text-green-700" : row.status === "failed" || row.status === "stale" || row.status === "rejected" ? "bg-red-50 text-red-700" : row.status === "approved" ? "bg-indigo-50 text-indigo-700" : "bg-amber-50 text-amber-700"}`}>{row.status}</span>}{row.error_message && <div className="mt-1 max-w-xs text-[10px] text-red-600">{row.error_message}</div>}</td>
              <td className="px-3 py-3"><div className="font-medium capitalize text-gray-800">{row.operation} · {row.target_name}</div><div className="mt-1 font-mono text-[10px] text-gray-400">{row.id}</div>{row.rollback_of && <div className="text-[10px] text-indigo-500">rollback of {row.rollback_of}</div>}</td>
              <td className="px-3 py-3"><span className={`rounded px-2 py-0.5 ${row.risk === "critical" ? "bg-red-100 text-red-700" : row.risk === "high" ? "bg-orange-100 text-orange-700" : "bg-amber-100 text-amber-700"}`}>{row.risk}</span></td>
              <td className="max-w-sm px-3 py-3 text-gray-600"><div>{row.summary.reason || "No reason provided"}</div>{row.summary.clone_source_id && <div className="mt-1 text-[10px] text-indigo-600" title={row.summary.clone_source_id}>Cloned from {row.summary.clone_source_name || row.summary.clone_source_id}</div>}<div className="mt-1 text-[10px] text-gray-400">Sensitive destinations, callbacks, and query payloads are encrypted and omitted from this audit view.</div></td>
              <td className="px-3 py-3 text-gray-600"><div>{new Date(row.requested_at).toLocaleString()}</div><div className="text-[10px] text-gray-400">by {row.requested_by}</div></td>
              <td className="px-3 py-3"><div className="flex flex-wrap gap-1">{row.status === "pending" && caps?.can_approve && <><button disabled={!!busy} onClick={() => onDecision(row, "approved")} className="rounded border border-green-300 px-2 py-1 text-[10px] text-green-700">Approve</button><button disabled={!!busy} onClick={() => onDecision(row, "rejected")} className="rounded border border-red-300 px-2 py-1 text-[10px] text-red-700">Reject</button><button disabled={!!busy} onClick={() => void showDetails(row)} className="rounded border px-2 py-1 text-[10px] text-gray-700">Details</button></>}{(row.status === "approved" || row.can_retry) && caps?.can_approve && <><button disabled={!!busy} onClick={() => onApply(row)} className="rounded bg-gray-900 px-2 py-1 text-[10px] text-white disabled:opacity-60">{applyingIds.has(row.id) ? "Applying…" : row.can_retry ? "Retry clone" : "Apply to Azure"}</button><button disabled={!!busy} onClick={() => void showDetails(row)} className="rounded border px-2 py-1 text-[10px] text-gray-700">Details</button></>}{row.status === "applied" && caps?.can_delete && <button disabled={!!busy} onClick={() => onRollback(row)} className="rounded border px-2 py-1 text-[10px] text-indigo-700">Prepare rollback</button>}{row.evidence_id && <a href="/evidence" className="rounded border px-2 py-1 text-[10px] text-gray-600">Evidence</a>}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No managed changes have been requested.</div>}
      </div>
      {detailsRow && <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-labelledby="managed-change-details-title"><div className="max-h-[92vh] w-full max-w-5xl overflow-auto rounded-xl bg-white shadow-2xl"><header className="sticky top-0 z-10 flex items-start gap-3 border-b bg-white px-5 py-4"><div><h2 id="managed-change-details-title" className="font-semibold text-gray-900">Apply to Azure details</h2><p className="mt-0.5 text-xs text-gray-500">Exact reviewed operation for {detailsRow.target_name}. No action is performed from this dialog.</p></div><button onClick={() => setDetailsRow(null)} className="ml-auto rounded px-2 py-1 text-gray-500">✕</button></header><div className="space-y-4 p-5 text-xs">{detailsLoading && <div className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-sky-700">Loading reviewed change details…</div>}{detailsError && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-red-700">{detailsError}</div>}{details && <><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><DetailField label="Azure method" value={details.execution.azure_method} /><DetailField label="Operation" value={details.execution.operation} /><DetailField label="Target type" value={details.execution.target_type} /><DetailField label="State" value={details.execution.ready_to_apply ? "Approved — ready to apply" : "Pending approval"} /></div><div className="rounded-lg border bg-gray-50 p-3"><div className="text-[10px] font-semibold uppercase text-gray-500">Target Azure resource</div><div className="mt-1 break-all font-mono text-[11px] text-gray-800">{details.execution.target_id}</div>{details.execution.expected_state_hash && <div className="mt-2 break-all text-[9px] text-gray-400">Concurrency hash: {details.execution.expected_state_hash}</div>}</div><div className="grid gap-4 lg:grid-cols-2"><JsonDetails title="Current Azure state" value={details.before} empty="No existing resource; Apply will create it." /><JsonDetails title="Resulting Azure request body" value={details.azure_body} empty={details.execution.azure_method === "DELETE" ? "DELETE sends no request body. The target resource will be removed." : "No ARM body stored."} /></div>{Object.keys(details.desired_payload).length > 0 && <JsonDetails title="Validated desired configuration" value={details.desired_payload} empty="" />}<div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-800">{details.redaction_notice}</div></>}</div><footer className="sticky bottom-0 flex justify-end border-t bg-white px-5 py-3"><button onClick={() => setDetailsRow(null)} className="rounded border px-3 py-1.5 text-xs">Close</button></footer></div></div>}
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border p-3"><div className="text-[10px] font-semibold uppercase text-gray-500">{label}</div><div className="mt-1 font-medium text-gray-800">{value || "—"}</div></div>;
}

function JsonDetails({ title, value, empty }: { title: string; value: Record<string, unknown>; empty: string }) {
  const hasValue = Object.keys(value).length > 0;
  return <section className="overflow-hidden rounded-lg border"><h3 className="border-b bg-gray-50 px-3 py-2 font-semibold text-gray-800">{title}</h3>{hasValue ? <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[10px] leading-5 text-gray-700">{JSON.stringify(value, null, 2)}</pre> : <div className="p-3 text-gray-500">{empty}</div>}</section>;
}

function ActionGroupEditor({ initial, connectionId, busy, saveError, onClose, onSave }: { initial: EditableActionGroup; connectionId: string; busy: boolean; saveError: string; onClose: () => void; onSave: (value: EditableActionGroup, reason: string) => void }) {
  const [value, setValue] = useState<EditableActionGroup>(initial);
  const [reason, setReason] = useState(initial.id ? "Update Action Group configuration" : initial.clone_source_id ? "Clone Action Group" : "Create Action Group");
  const [attempted, setAttempted] = useState(false);
  const requiredErrors = [
    ...(!initial.id && !value.name.trim() ? ["Name is required."] : []),
    ...(!initial.id && !value.subscription_id.trim() ? ["Subscription is required."] : []),
    ...(!initial.id && !value.resource_group.trim() ? ["Resource group is required."] : []),
    ...(!value.location.trim() ? ["Processing region is required."] : []),
    ...(!value.short_name.trim() ? ["Short name is required."] : []),
    ...(!reason.trim() ? ["Reason is required."] : []),
  ];
  const submit = () => {
    setAttempted(true);
    if (!requiredErrors.length) onSave(value, reason);
  };
  const updateList = <K extends "email_receivers" | "sms_receivers" | "webhook_receivers" | "arm_role_receivers">(key: K, index: number, patch: Partial<EditableActionGroup[K][number]>) => {
    setValue((current) => ({ ...current, [key]: current[key].map((item, i) => i === index ? { ...item, ...patch } : item) }));
  };
  const remove = (key: "email_receivers" | "sms_receivers" | "webhook_receivers" | "arm_role_receivers", index: number) => setValue((current) => ({ ...current, [key]: current[key].filter((_item, i) => i !== index) }));
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={initial.id ? "Edit Action Group" : initial.clone_source_id ? "Clone Action Group" : "Create Action Group"}>
      <div className="max-h-[92vh] w-full max-w-4xl overflow-auto rounded-xl bg-white shadow-xl">
        <div className="sticky top-0 z-10 flex items-center border-b bg-white px-5 py-4"><div><h2 className="font-semibold text-gray-900">{initial.id ? "Edit Action Group" : initial.clone_source_id ? "Clone Action Group" : "Create Action Group"}</h2><p className="text-xs text-gray-500">Saving creates an encrypted, approval-gated change. It does not immediately modify Azure.</p></div><button onClick={onClose} className="ml-auto rounded px-2 py-1 text-gray-500">✕</button></div>
        <div className="space-y-5 p-5 text-xs">
          {saveError && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-red-700"><div className="font-medium">Could not save this Action Group</div><div className="mt-1 whitespace-pre-wrap">{saveError}</div></div>}
          {attempted && requiredErrors.length > 0 && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-red-700"><div className="font-medium">Complete the required fields:</div><ul className="mt-1 list-disc pl-5">{requiredErrors.map((error) => <li key={error}>{error}</li>)}</ul></div>}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><label>Name{!initial.id && <span className="text-red-600"> *</span>}<input required={!initial.id} aria-required={!initial.id} disabled={!!initial.id} value={value.name} onChange={(e) => setValue({ ...value, name: e.target.value })} className="mt-1 w-full rounded border px-2 py-1.5 disabled:bg-gray-100" /></label><AzurePlacementFields connectionId={connectionId} subscriptionId={value.subscription_id} resourceGroup={value.resource_group} location={value.location} disabled={!!initial.id} required={!initial.id} globalLocation onChange={(patch) => setValue({ ...value, ...patch })} /><label>Short name <span className="text-red-600">*</span><input required aria-required="true" maxLength={12} value={value.short_name} onChange={(e) => setValue({ ...value, short_name: e.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label className="flex items-end gap-2 pb-1"><input type="checkbox" checked={value.enabled} onChange={(e) => setValue({ ...value, enabled: e.target.checked })} /> Enabled</label></div>
          <ReceiverSection title="Email receivers" onAdd={() => setValue({ ...value, email_receivers: [...value.email_receivers, { name: "", email_address: "", use_common_alert_schema: true }] })}>{value.email_receivers.map((item, i) => <div key={i} className="grid gap-2 sm:grid-cols-[1fr_2fr_auto_auto]"><input aria-label="Receiver name" placeholder="Name" value={item.name} onChange={(e) => updateList("email_receivers", i, { name: e.target.value })} className="rounded border px-2 py-1.5" /><input aria-label="Email address" placeholder="name@example.com" value={item.email_address} onChange={(e) => updateList("email_receivers", i, { email_address: e.target.value })} className="rounded border px-2 py-1.5" /><label className="flex items-center gap-1"><input type="checkbox" checked={item.use_common_alert_schema} onChange={(e) => updateList("email_receivers", i, { use_common_alert_schema: e.target.checked })} /> Common schema</label><button onClick={() => remove("email_receivers", i)} className="text-red-600">Remove</button></div>)}</ReceiverSection>
          <ReceiverSection title="SMS receivers" onAdd={() => setValue({ ...value, sms_receivers: [...value.sms_receivers, { name: "", country_code: "1", phone_number: "" }] })}>{value.sms_receivers.map((item, i) => <div key={i} className="grid gap-2 sm:grid-cols-[1fr_100px_2fr_auto]"><input aria-label="Receiver name" placeholder="Name" value={item.name} onChange={(e) => updateList("sms_receivers", i, { name: e.target.value })} className="rounded border px-2 py-1.5" /><input aria-label="Country code" placeholder="1" value={item.country_code} onChange={(e) => updateList("sms_receivers", i, { country_code: e.target.value })} className="rounded border px-2 py-1.5" /><input aria-label="Phone number" placeholder="Phone number" value={item.phone_number} onChange={(e) => updateList("sms_receivers", i, { phone_number: e.target.value })} className="rounded border px-2 py-1.5" /><button onClick={() => remove("sms_receivers", i)} className="text-red-600">Remove</button></div>)}</ReceiverSection>
          <ReceiverSection title="Webhook receivers" onAdd={() => setValue({ ...value, webhook_receivers: [...value.webhook_receivers, { name: "", service_uri: "", preserve_secret: false, use_common_alert_schema: true }] })}>{value.webhook_receivers.map((item, i) => <div key={i} className="grid gap-2 sm:grid-cols-[1fr_2fr_auto_auto]"><input aria-label="Receiver name" placeholder="Name" value={item.name} onChange={(e) => updateList("webhook_receivers", i, { name: e.target.value })} className="rounded border px-2 py-1.5" /><input aria-label="Webhook URL" placeholder="https://endpoint.example/path" value={item.service_uri} onChange={(e) => updateList("webhook_receivers", i, { service_uri: e.target.value, preserve_secret: false })} className="rounded border px-2 py-1.5" /><label className="flex items-center gap-1"><input type="checkbox" checked={item.use_common_alert_schema} onChange={(e) => updateList("webhook_receivers", i, { use_common_alert_schema: e.target.checked })} /> Common schema</label><button onClick={() => remove("webhook_receivers", i)} className="text-red-600">Remove</button>{item.preserve_secret && <span className="sm:col-span-4 text-[10px] text-amber-700">Signed query/secret is hidden and will be preserved unless this URL is changed.</span>}</div>)}</ReceiverSection>
          <ReceiverSection title="ARM role receivers" onAdd={() => setValue({ ...value, arm_role_receivers: [...value.arm_role_receivers, { name: "", role_id: "", use_common_alert_schema: true }] })}>{value.arm_role_receivers.map((item, i) => <div key={i} className="grid gap-2 sm:grid-cols-[1fr_2fr_auto_auto]"><input aria-label="Receiver name" placeholder="Name" value={item.name} onChange={(e) => updateList("arm_role_receivers", i, { name: e.target.value })} className="rounded border px-2 py-1.5" /><input aria-label="Role ID" placeholder="Built-in role definition GUID" value={item.role_id} onChange={(e) => updateList("arm_role_receivers", i, { role_id: e.target.value })} className="rounded border px-2 py-1.5" /><label className="flex items-center gap-1"><input type="checkbox" checked={item.use_common_alert_schema} onChange={(e) => updateList("arm_role_receivers", i, { use_common_alert_schema: e.target.checked })} /> Common schema</label><button onClick={() => remove("arm_role_receivers", i)} className="text-red-600">Remove</button></div>)}</ReceiverSection>
          <AdvancedActionGroupReceivers value={value} connectionId={connectionId} setValue={setValue} />
          <label className="block">Reason <span className="text-red-600">*</span><textarea required aria-required="true" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} className="mt-1 w-full rounded border px-2 py-1.5" /></label>
        </div>
        <div className="sticky bottom-0 flex items-center justify-end gap-2 border-t bg-white px-5 py-3"><span className="mr-auto text-[10px] text-gray-500"><span className="text-red-600">*</span> Required fields</span><button onClick={onClose} className="rounded border px-3 py-1.5 text-xs">Cancel</button><button disabled={busy} onClick={submit} className="rounded bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{busy ? "Saving…" : "Create change request"}</button></div>
      </div>
    </div>
  );
}

function ReceiverSection({ title, onAdd, children }: { title: string; onAdd: () => void; children: ReactNode }) {
  return <section className="space-y-2 rounded-lg border p-3"><div className="flex items-center"><h3 className="font-semibold text-gray-800">{title}</h3><button onClick={onAdd} className="ml-auto rounded border px-2 py-1 text-[10px] text-indigo-700">+ Add</button></div>{children}<>{!children && <div className="text-gray-400">None configured.</div>}</></section>;
}

export function AlertsManagerPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { tab: routeTab } = useParams<{ tab?: string }>();
  const [routeSearch] = useSearchParams();
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription" | "management_group">("azsup.alertAnalysis.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.alertAnalysis.workloadId", "");
  useWorkloadDeepLink(setScopeKind, setWorkloadId);
  const [subId, setSubId] = usePersistedState("azsup.alertAnalysis.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.alertAnalysis.subName", "");
  const [connId, setConnId] = usePersistedState("azsup.alertAnalysis.connectionId", "");
  const [mgId, setMgId] = usePersistedState("azsup.alertAnalysis.mgId", "");
  const [mgName, setMgName] = usePersistedState("azsup.alertAnalysis.mgName", "");
  const tab: Tab = routeTab && VALID_TABS.has(routeTab as Tab) ? routeTab as Tab : "overview";
  const contentScrollRef = useRef<HTMLElement>(null);
  const page = Math.max(1, Number.parseInt(routeSearch.get("page") || "1", 10) || 1);
  const [changesView, setChangesView] = useState<ChangesView>("action_required");
  const [changesSort, setChangesSort] = useState<ChangesSort>("newest");
  function goTab(next: Tab) {
    navigate(`/alerts-manager/${next}`);
    if (next === "changes") requestAnimationFrame(() => contentScrollRef.current?.scrollTo({ top: 0, behavior: "auto" }));
  }
  function goPage(next: number) {
    const params = new URLSearchParams(routeSearch);
    if (next <= 1) params.delete("page"); else params.set("page", String(next));
    navigate({ pathname: `/alerts-manager/${tab}`, search: params.toString() ? `?${params}` : "" });
  }
  useEffect(() => {
    if (!routeTab || !VALID_TABS.has(routeTab as Tab)) navigate("/alerts-manager/overview", { replace: true });
  }, [navigate, routeTab]);
  useEffect(() => {
    const linkedConnectionId = routeSearch.get("connection_id");
    if (linkedConnectionId && linkedConnectionId !== connId) setConnId(linkedConnectionId);
  }, [connId, routeSearch, setConnId]);
  useEffect(() => {
    if (tab === "changes") contentScrollRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [tab]);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [ruleSort, setRuleSort] = useState<"default" | "cost_desc" | "cost_asc">("default");
  const [analysisNeedsRefresh, setAnalysisNeedsRefresh] = useState(false);
  const [exporting, setExporting] = useState("");
  const [error, setError] = useState("");
  const [editor, setEditor] = useState<EditableActionGroup | null>(null);
  const [actionGroupEditorError, setActionGroupEditorError] = useState("");
  const [ruleEditor, setRuleEditor] = useState<EditableAlertRule | null>(null);
  const [managementBusy, setManagementBusy] = useState("");
  const [applyingChangeIds, setApplyingChangeIds] = useState<Set<string>>(new Set());
  const [bulkPreparingCount, setBulkPreparingCount] = useState(0);
  const [selectedGapIds, setSelectedGapIds] = useState<Set<string>>(new Set());
  const [gapPlannerOpen, setGapPlannerOpen] = useState(false);
  const [activityLogWizardOpen, setActivityLogWizardOpen] = useState(false);
  const [activityLogDiagnosticsOpen, setActivityLogDiagnosticsOpen] = useState(false);
  const [focusedDeploymentPlanId, setFocusedDeploymentPlanId] = useState("");

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = workloadsQ.data?.workloads ?? [];
  const connectionWorkloads = workloads.filter((workload) => !connId || !workload.connection_id || workload.connection_id === connId);
  const effectiveWorkloadId = scopeKind === "workload"
    ? connectionWorkloads.some((workload) => workload.id === workloadId)
      ? workloadId
      : connectionWorkloads.find((workload) => workload.id === "demo-amba-coverage")?.id || connectionWorkloads[0]?.id || ""
    : "";
  const params = scopeKind === "workload"
    ? { workload_id: effectiveWorkloadId, connection_id: connId }
    : scopeKind === "subscription"
      ? { subscription_id: subId, connection_id: connId }
      : { management_group_id: mgId, connection_id: connId };
  const ready = scopeKind === "workload" ? !!effectiveWorkloadId : scopeKind === "subscription" ? !!subId : !!mgId;
  const queryKey = ["alert-analysis", scopeKind, effectiveWorkloadId, subId, mgId, connId];
  const reportQ = useQuery({
    queryKey,
    queryFn: () => api.alertAnalysis(params),
    enabled: ready,
    staleTime: Infinity,
  });
  const data = reportQ.data;
  const jobKey = ["alert-analysis-refresh-job", scopeKind, effectiveWorkloadId, subId, mgId, connId];
  const jobQ = useQuery({
    queryKey: jobKey,
    queryFn: () => api.alertAnalysisRefreshJob(params),
    enabled: ready,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: (query) => query.state.data?.job?.status === "running" ? 1000 : false,
  });
  const refreshJob = jobQ.data?.job;
  const refreshing = refreshJob?.status === "running";
  const handledJobsRef = useRef(new Set<string>());
  const activeJobsRef = useRef(new Set<string>());
  const trendQ = useQuery({
    queryKey: ["alert-analysis-trend", scopeKind, effectiveWorkloadId, subId, mgId],
    queryFn: () => api.alertAnalysisTrend(params),
    enabled: ready && !!data?.report_exists,
    staleTime: 5 * 60 * 1000,
  });
  const managementParams = scopeKind === "workload"
    ? { workload_id: effectiveWorkloadId, connection_id: connId }
    : scopeKind === "subscription"
      ? { subscription_id: subId, connection_id: connId }
      : { management_group_id: mgId, connection_id: connId };
  const firedQ = useQuery({
    queryKey: queryKeys.alertsManager.inbox(managementParams, 30),
    queryFn: () => api.firedAlertInstances({ ...managementParams, days: 30 }),
    enabled: ready && !!data && !data.demo && tab === "inbox",
    staleTime: 2 * 60_000,
  });
  const managedGroupsQ = useQuery({
    queryKey: queryKeys.alertsManager.actionGroups(managementParams),
    queryFn: () => api.managedActionGroups(managementParams),
    enabled: ready && !!data && !data.demo && (tab === "action-groups" || gapPlannerOpen || activityLogWizardOpen),
    staleTime: 5 * 60_000,
  });
  const ruleActionGroupsQ = useQuery({
    queryKey: queryKeys.alertsManager.actionGroups({ ...managementParams, all_visible: true }),
    queryFn: () => api.managedActionGroups({ ...managementParams, all_visible: true }),
    enabled: ready && !!data && !data.demo,
    staleTime: 5 * 60_000,
  });
  const managedRulesQ = useQuery({
    queryKey: queryKeys.alertsManager.rules(managementParams),
    queryFn: () => api.managedAlertRules(managementParams),
    enabled: ready && !!data && !data.demo,
    staleTime: 5 * 60_000,
  });
  const summaryQ = useQuery({
    queryKey: queryKeys.alertsManager.summary(connId),
    queryFn: () => api.alertsManagerSummary(connId),
    enabled: ready && !!data && !data.demo,
    staleTime: 30_000,
  });
  const activityLogCoverageQ = useQuery({
    queryKey: queryKeys.alertsManager.activityLogCoverage(managementParams),
    queryFn: () => api.activityLogCoverage(managementParams),
    enabled: ready && !!data && !data.demo && (tab === "overview" || activityLogWizardOpen),
    staleTime: 5 * 60_000,
  });
  const capabilities = summaryQ.data?.capabilities;
  const capabilitiesQ = { data: capabilities };
  const latestAppliedAt = summaryQ.data?.latest_applied_at ? Date.parse(ensureUtc(summaryQ.data.latest_applied_at)) : Number.NaN;
  const analysisGeneratedAt = data?.generated_at ? Date.parse(ensureUtc(data.generated_at)) : Number.NaN;
  const analysisIsStaleAfterApply = analysisNeedsRefresh || (Number.isFinite(latestAppliedAt) && Number.isFinite(analysisGeneratedAt) && latestAppliedAt > analysisGeneratedAt);
  const changesQ = useQuery({
    queryKey: queryKeys.alertsManager.changes(connId, page, CHANGES_PAGE_SIZE, changesView, changesSort),
    queryFn: () => api.alertsManagerChanges(connId, page, CHANGES_PAGE_SIZE, changesView, changesSort),
    enabled: ready && !!data && !data.demo && tab === "changes",
    staleTime: 15_000,
  });
  const changesQueryKey = queryKeys.alertsManager.changes(connId, page, CHANGES_PAGE_SIZE, changesView, changesSort);
  useEffect(() => {
    const state = jobQ.data;
    if (!state?.job) return;
    const identity = `${scopeKind}:${effectiveWorkloadId || subId || mgId}:${connId}:${state.job.id}`;
    if (state.job.status === "running") {
      activeJobsRef.current.add(identity);
      return;
    }
    if (handledJobsRef.current.has(identity)) return;
    handledJobsRef.current.add(identity);
    if (!activeJobsRef.current.delete(identity)) return;
    if (state.job.status === "done" && state.result) {
      qc.setQueryData(queryKey, state.result);
      setAnalysisNeedsRefresh(false);
      void Promise.all([
        qc.invalidateQueries({ queryKey: ["alert-analysis-trend"] }),
        qc.invalidateQueries({ queryKey: ["alert-analysis-runs"] }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.rulesRoot }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.actionGroupsRoot }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.activityLogCoverageRoot }),
      ]);
    } else if (state.job.status === "error") {
      setError(state.job.error || "Alert analysis failed.");
    }
  }, [jobQ.data, qc, queryKey, scopeKind, effectiveWorkloadId, subId, mgId, connId]);

  const query = useDeferredValue(search.trim().toLowerCase());
  const searchableRules = useMemo(() => (data?.rules ?? []).map((rule) => ({ rule, text: `${rule.name} ${rule.type} ${rule.resource_group} ${rule.conditions.map((item) => item.signal_name).join(" ")} ${rule.action_group_names.join(" ")}`.toLowerCase() })), [data?.rules]);
  const searchableOverlaps = useMemo(() => (data?.active_overlaps ?? data?.overlaps ?? []).map((row) => ({ row, text: `${row.signal_name} ${row.target_id} ${row.rule_names.join(" ")}`.toLowerCase() })), [data?.active_overlaps, data?.overlaps]);
  const searchableGaps = useMemo(() => (data?.active_gaps ?? data?.gaps ?? []).map((row) => ({ row, text: `${row.type} ${row.resource_name} ${row.rule_name} ${row.signal}`.toLowerCase() })), [data?.active_gaps, data?.gaps]);
  const rules = useMemo(() => {
    const filtered = searchableRules.filter(({ rule, text }) => {
      if (status !== "all" && rule.finding_status !== status) return false;
      return !query || text.includes(query);
    }).map(({ rule }) => rule);
    if (ruleSort === "default") return filtered;
    return [...filtered].sort((left, right) => {
      const leftCost = left.cost?.monthly_usd;
      const rightCost = right.cost?.monthly_usd;
      if (leftCost == null && rightCost == null) return left.name.localeCompare(right.name);
      if (leftCost == null) return 1;
      if (rightCost == null) return -1;
      return ruleSort === "cost_desc" ? rightCost - leftCost : leftCost - rightCost;
    });
  }, [searchableRules, query, status, ruleSort]);
  const overlaps = useMemo(() => searchableOverlaps.filter(({ text }) => !query || text.includes(query)).map(({ row }) => row), [searchableOverlaps, query]);
  const gaps = useMemo(() => searchableGaps.filter(({ text }) => !query || text.includes(query)).map(({ row }) => row), [searchableGaps, query]);
  const allGaps = data?.active_gaps ?? data?.gaps ?? [];
  const allGapIds = useMemo(() => [...new Set(allGaps.map((row, index) => gapIdentity(row, index)))], [allGaps]);
  const gapPlansQ = useQuery({
    queryKey: ["alerts-manager-deployment-plans-by-gap", allGapIds],
    queryFn: () => api.deploymentPlansByGap(allGapIds),
    enabled: ready && !!data && !data.demo && tab === "gaps" && allGapIds.length > 0,
    staleTime: Infinity,
  });
  useEffect(() => {
    const analysisKey = ["alert-analysis", scopeKind, effectiveWorkloadId, subId, mgId, connId];
    const rulesKey = queryKeys.alertsManager.rules(managementParams);
    const groupsKey = queryKeys.alertsManager.actionGroups(managementParams);
    const allVisibleGroupsKey = queryKeys.alertsManager.actionGroups({ ...managementParams, all_visible: true });
    const inboxKey = queryKeys.alertsManager.inbox(managementParams, 30);
    return () => {
      void Promise.all([
        qc.cancelQueries({ queryKey: analysisKey, exact: true }),
        qc.cancelQueries({ queryKey: rulesKey, exact: true }),
        qc.cancelQueries({ queryKey: groupsKey, exact: true }),
        qc.cancelQueries({ queryKey: allVisibleGroupsKey, exact: true }),
        qc.cancelQueries({ queryKey: inboxKey, exact: true }),
        qc.cancelQueries({ queryKey: queryKeys.alertsManager.activityLogCoverage(managementParams), exact: true }),
      ]);
    };
  }, [qc, scopeKind, effectiveWorkloadId, subId, mgId, connId]);
  useEffect(() => {
    const statuses = gapPlansQ.data?.by_gap;
    if (!statuses) return;
    setSelectedGapIds((current) => {
      const next = new Set([...current].filter((id) => !isActiveGapPlanStatus(statuses[id]?.status)));
      return next.size === current.size ? current : next;
    });
  }, [gapPlansQ.data]);
  const analysisSelectionKey = `${data?.scope_kind ?? scopeKind}:${data?.scope_id ?? ""}:${data?.generated_at ?? ""}`;
  useEffect(() => { setSelectedGapIds(new Set()); setGapPlannerOpen(false); setAnalysisNeedsRefresh(false); }, [analysisSelectionKey]);
  const selectedGaps = useMemo(() => {
    const unique = new Map<string, AlertAnalysisGap>();
    for (const [index, row] of allGaps.entries()) {
      const id = gapIdentity(row, index);
      if (selectedGapIds.has(id) && !unique.has(id)) unique.set(id, row);
    }
    return [...unique.values()];
  }, [allGaps, selectedGapIds]);
  const canPlanGaps = !!capabilities?.can_submit_deployment_plans && !capabilities.read_only && !data?.demo;
  const invalidateManagedChanges = () => Promise.all([
    qc.invalidateQueries({ queryKey: queryKeys.alertsManager.changesRoot }),
    qc.invalidateQueries({ queryKey: queryKeys.alertsManager.summaryRoot }),
  ]);

  async function refresh() {
    if (!ready || refreshing) return;
    setError("");
    try {
      const state = await api.startAlertAnalysisRefresh(params);
      if (state.job) {
        const identity = `${scopeKind}:${effectiveWorkloadId || subId || mgId}:${connId}:${state.job.id}`;
        activeJobsRef.current.add(identity);
        handledJobsRef.current.delete(identity);
      }
      qc.setQueryData(jobKey, state);
    } catch (cause) {
      setError(formatError(cause));
    }
  }

  async function download(format: "csv" | "json" | "xlsx") {
    if (!data || exporting) return;
    setExporting(format); setError("");
    try {
      const blob = await api.exportAlertAnalysis(params, format);
      const safe = (data.scope_name || "scope").replace(/[^A-Za-z0-9_.-]+/g, "-");
      downloadBlob(blob, `alerts-manager-${safe}.${format}`);
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setExporting("");
    }
  }

  async function saveEvidence() {
    if (!data?.report_exists || exporting) return;
    setExporting("evidence"); setError("");
    try {
      const result = await api.captureAlertAnalysisEvidence(params);
      alert(`Saved to Evidence Locker: ${result.snapshot.name}`);
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setExporting("");
    }
  }

  async function recordDecision(targetType: "rule" | "overlap" | "gap", targetId: string, action: string) {
    const reason = window.prompt("Reason for this decision (stored in the audit trail):", "Reviewed and accepted by operator") ?? "";
    if (!reason) return;
    setError("");
    try {
      await api.recordAlertAnalysisDecision(connId, { target_type: targetType, target_id: targetId, action, reason });
      await qc.invalidateQueries({ queryKey: ["alert-analysis"] });
    } catch (cause) {
      setError(formatError(cause));
    }
  }


  function blankActionGroup(): EditableActionGroup {
    return {
      name: "", subscription_id: scopeKind === "subscription" ? subId : "", resource_group: "",
      location: "Global", short_name: "", enabled: true, email_receivers: [], sms_receivers: [],
      webhook_receivers: [], arm_role_receivers: [], voice_receivers: [], azure_app_push_receivers: [],
      azure_function_receivers: [], logic_app_receivers: [], event_hub_receivers: [],
      automation_runbook_receivers: [], itsm_receivers: [], tags: {},
    };
  }

  async function editActionGroup(group: ManagedActionGroup) {
    setManagementBusy(group.id); setError(""); setActionGroupEditorError("");
    try {
      const result = await api.managedActionGroupDetails(connId, group.id);
      setEditor(result.action_group);
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function cloneActionGroup(group: ManagedActionGroup) {
    setManagementBusy(group.id); setError(""); setActionGroupEditorError("");
    try {
      const source = (await api.managedActionGroupDetails(connId, group.id)).action_group;
      const existingNames = new Set((managedGroupsQ.data?.action_groups ?? []).map((item) => item.name.toLowerCase()));
      const baseName = source.name.replace(/-copy(?:-\d+)?$/i, "");
      let copyNumber = 1;
      let cloneName = `${baseName}-copy`;
      while (existingNames.has(cloneName.toLowerCase())) cloneName = `${baseName}-copy-${++copyNumber}`;
      const shortBase = source.short_name || source.name;
      const shortSuffix = copyNumber === 1 ? "-cp" : `-cp${copyNumber}`;
      setEditor({
        ...source,
        id: undefined,
        clone_source_id: group.id,
        name: cloneName,
        short_name: `${shortBase.slice(0, 12 - shortSuffix.length)}${shortSuffix}`,
        state_hash: "",
      });
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function saveActionGroup(value: EditableActionGroup, reason: string) {
    setManagementBusy("save"); setError(""); setActionGroupEditorError("");
    try {
      await api.requestActionGroupChange({ connection_id: connId, operation: value.id ? "update" : "create", target_id: value.id, clone_source_id: value.clone_source_id, desired: value, reason });
      setEditor(null); goTab("changes");
      await Promise.all([
        invalidateManagedChanges(),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.actionGroupsRoot }),
      ]);
    } catch (cause) { setActionGroupEditorError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function toggleActionGroup(group: ManagedActionGroup) {
    setManagementBusy(group.id); setError("");
    try {
      const details = (await api.managedActionGroupDetails(connId, group.id)).action_group;
      await api.requestActionGroupChange({ connection_id: connId, operation: "update", target_id: group.id, desired: { ...details, enabled: !group.enabled }, reason: `${group.enabled ? "Disable" : "Enable"} Action Group` });
      goTab("changes"); await invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function deleteActionGroup(group: ManagedActionGroup) {
    if (!window.confirm(`Request deletion of ${group.name}? Azure will not be changed until this is approved and applied.`)) return;
    setManagementBusy(group.id); setError("");
    try {
      await api.requestActionGroupChange({ connection_id: connId, operation: "delete", target_id: group.id, reason: "Delete unused Action Group" });
      goTab("changes"); await invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function testActionGroup(group: ManagedActionGroup) {
    const confirmation = window.prompt(`This sends REAL test notifications to ${group.receiver_count} receiver(s). Type SEND TEST to continue:`, "") ?? "";
    if (!confirmation) return;
    setManagementBusy(group.id); setError("");
    try {
      const result = await api.testManagedActionGroup(connId, group.id, "metricstaticthreshold", confirmation);
      alert(`Action Group test: ${result.state}\n${result.details.map((item) => `${item.mechanism} ${item.name}: ${item.status}${item.detail ? ` — ${item.detail}` : ""}`).join("\n")}`);
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function changeAlertState(row: FiredAlertInstance, state: "New" | "Acknowledged" | "Closed") {
    setManagementBusy(row.id); setError("");
    try {
      await api.changeFiredAlertState(connId, row.id, state);
      await qc.invalidateQueries({ queryKey: ["alerts-manager-inbox"] });
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function decideManagedChange(row: AlertsManagerChange, decision: "approved" | "rejected") {
    const reason = window.prompt(`${decision === "approved" ? "Approval" : "Rejection"} reason:`, "Reviewed by operator") ?? "";
    if (!reason) return;
    setManagementBusy(row.id); setError("");
    try { await api.decideAlertsManagerChange(row.id, decision, reason); await invalidateManagedChanges(); }
    catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function bulkDecideManagedChanges(rows: AlertsManagerChange[], decision: "approved" | "rejected") {
    if (!rows.length) return;
    const reason = window.prompt(`${decision === "approved" ? "Approval" : "Rejection"} reason for ${rows.length} selected changes:`, `Bulk ${decision} after operator review`) ?? "";
    if (!reason) return;
    setManagementBusy("bulk-decision"); setError("");
    const failures: string[] = [];
    try {
      for (const row of rows) {
        try { await api.decideAlertsManagerChange(row.id, decision, reason); }
        catch (cause) { failures.push(`${row.target_name}: ${formatError(cause)}`); }
      }
      await invalidateManagedChanges();
      if (failures.length) setError(`${rows.length - failures.length} of ${rows.length} changes were ${decision}. ${failures.join(" · ")}`);
    } finally { setManagementBusy(""); }
  }

  async function applyManagedChange(row: AlertsManagerChange) {
    if (!window.confirm(row.can_retry ? `Retry the failed clone for ${row.target_name} now? The source receiver endpoints will be restored before applying.` : `Apply the approved ${row.operation} for ${row.target_name} to Azure now?`)) return;
    setManagementBusy(row.id); setApplyingChangeIds((current) => new Set(current).add(row.id)); setError("");
    try {
      const result = await api.applyAlertsManagerChange(row.id);
      setAnalysisNeedsRefresh(true);
      qc.setQueryData<ChangesPage>(changesQueryKey, (current) => current ? { ...current, changes: current.changes.map((change) => change.id === result.change.id ? result.change : change) } : current);
      await changesQ.refetch();
      void Promise.all([qc.invalidateQueries({ queryKey: queryKeys.alertsManager.summaryRoot }), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.actionGroupsRoot }), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.rulesRoot }), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.activityLogCoverageRoot })]);
      if (row.target_type === "action_group") {
        goTab("action-groups");
        await managedGroupsQ.refetch();
      }
    } catch (cause) { setError(formatError(cause)); await changesQ.refetch(); }
    finally { setApplyingChangeIds((current) => { const next = new Set(current); next.delete(row.id); return next; }); setManagementBusy(""); }
  }

  async function bulkApplyManagedChanges(rows: AlertsManagerChange[]) {
    if (!rows.length || !window.confirm(`Apply ${rows.length} approved changes to Azure with up to 6 concurrent workers? Each change remains independently audited and failures will not hide successful applications.`)) return;
    setManagementBusy("bulk-apply"); setError("");
    const failures: string[] = [];
    let appliedCount = 0;
    try {
      let cursor = 0;
      async function worker() {
        while (cursor < rows.length) {
          const row = rows[cursor++];
          setApplyingChangeIds((current) => new Set(current).add(row.id));
          try {
            const result = await api.applyAlertsManagerChange(row.id);
            appliedCount += 1;
            qc.setQueryData<ChangesPage>(changesQueryKey, (current) => current ? { ...current, changes: current.changes.map((change) => change.id === result.change.id ? result.change : change) } : current);
          } catch (cause) {
            failures.push(`${row.target_name}: ${formatError(cause)}`);
          } finally {
            setApplyingChangeIds((current) => { const next = new Set(current); next.delete(row.id); return next; });
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(6, rows.length) }, () => worker()));
      if (appliedCount > 0) setAnalysisNeedsRefresh(true);
      await Promise.all([
        invalidateManagedChanges(),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.summaryRoot }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.actionGroupsRoot }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.rulesRoot }),
        qc.invalidateQueries({ queryKey: queryKeys.alertsManager.activityLogCoverageRoot }),
      ]);
      if (page !== 1) goPage(1); else await changesQ.refetch();
      if (failures.length) setError(`${rows.length - failures.length} of ${rows.length} changes were applied. ${failures.join(" · ")}`);
    } finally { setApplyingChangeIds(new Set()); setManagementBusy(""); }
  }

  async function rollbackManagedChange(row: AlertsManagerChange) {
    if (!window.confirm(`Prepare an approval-gated rollback for ${row.target_name}?`)) return;
    setManagementBusy(row.id); setError("");
    try { await api.rollbackAlertsManagerChange(row.id); await invalidateManagedChanges(); }
    catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function editManagedRule(row: ManagedAlertRule) {
    setManagementBusy(row.id); setError("");
    try { setRuleEditor((await api.managedAlertRuleDetails(connId, row.id, row.family)).rule); }
    catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function cloneManagedRule(row: ManagedAlertRule) {
    setManagementBusy(row.id); setError("");
    try {
      const source = (await api.managedAlertRuleDetails(connId, row.id, row.family)).rule;
      setRuleEditor({ ...source, id: "", name: `${source.name}-copy`.slice(0, 120), enabled: false, state_hash: "" });
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function saveManagedRule(value: EditableAlertRule, reason: string) {
    setManagementBusy("rule-save"); setError("");
    try {
      await api.requestAlertRuleChange({ connection_id: connId, family: value.family, operation: value.id ? "update" : "create", target_id: value.id, desired: value, reason });
      setRuleEditor(null); goTab("changes"); await invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function toggleManagedRule(row: ManagedAlertRule) {
    setManagementBusy(row.id); setError("");
    try {
      const details = (await api.managedAlertRuleDetails(connId, row.id, row.family)).rule;
      await api.requestAlertRuleChange({ connection_id: connId, family: row.family, operation: "update", target_id: row.id, desired: { ...details, enabled: !row.enabled }, reason: `${row.enabled ? "Disable" : "Enable"} ${row.family} alert rule` });
      goTab("changes"); await invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function deleteManagedRule(row: ManagedAlertRule) {
    if (!window.confirm(`Request deletion of ${row.name}? Azure won't change until approval and Apply.`)) return;
    setManagementBusy(row.id); setError("");
    try {
      await api.requestAlertRuleChange({ connection_id: connId, family: row.family, operation: "delete", target_id: row.id, reason: "Delete Azure Monitor alert rule" });
      goTab("changes"); await invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setManagementBusy(""); }
  }

  async function bulkManagedRules(rows: ManagedAlertRule[], action: "enable" | "disable" | "delete" | "add_action_group", actionGroupId: string) {
    if (!rows.length) return;
    const reason = window.prompt(`Reason for ${action.replaceAll("_", " ")} on ${rows.length} rules:`, "Reviewed bulk alert-rule change") ?? "";
    if (!reason) return;
    if (action === "delete" && !window.confirm(`Create ${rows.length} independent deletion requests? No Azure resources change until each request is approved and applied.`)) return;
    setManagementBusy("bulk"); setBulkPreparingCount(rows.length); setError("");
    qc.setQueryData<ChangesPage>(queryKeys.alertsManager.changes(connId, 1, CHANGES_PAGE_SIZE), (current) => current ?? { changes: [], total: 0, page: 1, page_size: CHANGES_PAGE_SIZE, pending_count: 0, approved_count: 0, actionable_count: 0 });
    goTab("changes");
    try {
      const result = await api.bulkAlertRuleChanges({ connection_id: connId, action, action_group_id: actionGroupId, targets: rows.map((row) => ({ target_id: row.id, family: row.family })), reason });
      qc.setQueryData<ChangesPage>(queryKeys.alertsManager.changes(connId, 1, CHANGES_PAGE_SIZE), (current) => {
        const createdIds = new Set(result.changes.map((change) => change.id));
        const prior = current?.changes ?? [];
        return { changes: [...result.changes, ...prior.filter((change) => !createdIds.has(change.id))].slice(0, CHANGES_PAGE_SIZE), total: (current?.total ?? prior.length) + result.changes.length, page: 1, page_size: CHANGES_PAGE_SIZE, pending_count: (current?.pending_count ?? 0) + result.changes.length, approved_count: current?.approved_count ?? 0, actionable_count: (current?.actionable_count ?? 0) + result.changes.length };
      });
      void invalidateManagedChanges();
    } catch (cause) { setError(formatError(cause)); }
    finally { setBulkPreparingCount(0); setManagementBusy(""); }
  }

  function createManagedRule(family: RuleFamily) {
    setRuleEditor(newAlertRule(family, scopeKind === "subscription" ? subId : ""));
  }

  const tabs: { id: Tab; label: string; count?: number; urgent?: boolean }[] = [
    { id: "overview", label: "Overview" },
    { id: "inbox", label: "Alert instances", count: firedQ.data?.count },
    { id: "visualize", label: "Visualize" },
    { id: "overlaps", label: "Overlaps", count: data?.overlaps.length },
    { id: "gaps", label: "Gaps", count: data?.gaps.length },
    { id: "rules", label: "Rule analysis", count: data?.rules.length },
    { id: "manage-rules", label: "Rule management", count: managedRulesQ.data?.count },
    { id: "action-groups", label: "Action groups", count: data?.action_groups.length ?? managedGroupsQ.data?.count },
    { id: "deployment-plans", label: "Deployment plans" },
    { id: "changes", label: "Managed changes", count: summaryQ.data?.actionable_count ?? changesQ.data?.actionable_count, urgent: (summaryQ.data?.actionable_count ?? changesQ.data?.actionable_count ?? 0) > 0 },
  ];
  const managementTab = tab === "inbox" || tab === "manage-rules" || tab === "action-groups" || tab === "deployment-plans" || tab === "visualize" || tab === "changes";

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <header className="border-b bg-white px-4 py-4 sm:px-6">
        <div className="flex flex-wrap items-start gap-3 sm:gap-4">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-amber-100 to-orange-50 text-2xl">🔔</div>
          <div className="min-w-0 basis-[220px] flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <h1 className="text-lg font-semibold text-gray-900">Alerts Manager</h1>
              {data?.demo && <span className="rounded bg-indigo-50 px-2 py-0.5 text-[10px] text-indigo-700">demo data</span>}
              {data?.partial && <span className="rounded bg-amber-50 px-2 py-0.5 text-[10px] text-amber-700">partial result</span>}
              {data?.report_exists && <span className="text-[10px] font-normal text-gray-400">Updated {ageText(data.age_seconds)}{data.stale && <span className="text-amber-600"> · stale</span>} · cached</span>}
            </div>
            <p className="mt-0.5 max-w-3xl text-xs leading-4 text-gray-500">Find overlapping Azure Monitor rules, duplicate notification paths, and AMBA baseline coverage gaps. Recipient destinations are shown in full.</p>
          </div>
          {data?.report_exists && <div className="hidden w-full flex-none pl-14 sm:block sm:w-auto sm:pl-0"><TrendChart points={trendQ.data?.points ?? []} current={trendQ.data?.current} previous={trendQ.data?.previous} delta={trendQ.data?.delta} loading={trendQ.isLoading} deltaLabel="score vs last scan" /></div>}
          <div className="flex w-full flex-nowrap items-center gap-2 overflow-x-auto pb-1 xl:ml-auto xl:w-auto xl:flex-wrap xl:overflow-visible xl:pb-0">
            <ConnectionScopePicker value={connId} onChange={(id) => {
              if (id === connId) return;
              setConnId(id);
              setWorkloadId("");
              setSubId(""); setSubName("");
              setMgId(""); setMgName("");
            }} />
            <div className="flex items-center gap-2">
              <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
                {(["workload", "subscription", "management_group"] as const).map((kind) => (
                  <button key={kind} onClick={() => setScopeKind(kind)} className={`rounded-md px-2.5 py-1 ${scopeKind === kind ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}>
                    {kind === "workload" ? "Workload" : kind === "subscription" ? "Subscription" : "Management group"}
                  </button>
                ))}
              </div>
              {scopeKind === "management_group" ? (
                <ManagementGroupPicker value={mgId} valueName={mgName} connectionId={connId} onPick={(id, name) => { setMgId(id); setMgName(name); }} />
              ) : (
                <ScopePicker
                  scopeKind={scopeKind}
                  onScopeKindChange={() => {}}
                  workloads={connectionWorkloads}
                  workloadId={effectiveWorkloadId}
                  onWorkloadChange={setWorkloadId}
                  subId={subId}
                  subName={subName}
                  connectionId={connId}
                  onSubPick={(id, name) => { setSubId(id); setSubName(name); }}
                  workloadOnly={scopeKind === "workload"}
                  hideKindToggle
                />
              )}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => void refresh()} disabled={!ready || refreshing} title={analysisIsStaleAfterApply ? "Azure was changed after this analysis. Analyze again to refresh findings, costs, coverage, and counts." : undefined} className={`rounded-lg px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50 ${analysisIsStaleAfterApply ? "animate-pulse bg-red-600 ring-2 ring-red-200 hover:bg-red-700" : "bg-gray-900 hover:bg-gray-700"}`}>{refreshing ? "Analyzing…" : analysisIsStaleAfterApply ? "⚠ Data stale — Analyze again" : data?.report_exists ? "↻ Analyze again" : "Analyze alerts"}</button>
              {analysisIsStaleAfterApply && <span role="status" className="max-w-44 text-[10px] font-medium leading-tight text-red-600">Azure changed. Refresh this analysis for current data.</span>}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => void download("csv")} disabled={!data?.report_exists || !!exporting} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{exporting === "csv" ? "Exporting…" : "⬇ CSV"}</button>
              <button onClick={() => void download("xlsx")} disabled={!data?.report_exists || !!exporting} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{exporting === "xlsx" ? "Exporting…" : "📊 XLSX"}</button>
              <button onClick={() => void download("json")} disabled={!data?.report_exists || !!exporting} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">JSON</button>
              <button onClick={() => void saveEvidence()} disabled={!data?.report_exists || !!exporting} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{exporting === "evidence" ? "Saving…" : "🗄 Evidence"}</button>
            </div>
          </div>
        </div>
        {data?.report_exists && (
          <div className="mt-3 grid grid-cols-[repeat(auto-fit,minmax(min(92px,100%),1fr))] gap-2">
            <Kpi label="Score" value={data.rationalization_score} tone={data.rationalization_score >= 80 ? "green" : data.rationalization_score >= 50 ? "amber" : "red"} hint="Higher means fewer overlap and gap findings relative to this scope." />
            <Kpi label="Rules" value={data.kpis.total_rules} />
            <Kpi label="Overlap groups" value={data.kpis.overlap_groups} tone={data.kpis.overlap_groups > 0 ? "amber" : "gray"} />
            <Kpi label="Duplicate paths" value={data.kpis.notification_overlaps} tone={data.kpis.notification_overlaps > 0 ? "red" : "gray"} />
            <Kpi label="Gaps" value={data.kpis.gap_count} tone={data.kpis.gap_count > 0 ? "red" : "gray"} />
            <Kpi label="Action groups" value={data.kpis.action_groups} />
            <Kpi label="Recipients" value={data.kpis.unique_recipients} />
            <Kpi label="Recipient proliferation" value={data.kpis.recipient_proliferation} tone={data.kpis.recipient_proliferation > 0 ? "amber" : "gray"} />
            <Kpi label="Resources" value={data.kpis.resources_evaluated} />
            <Kpi label="Fires 7d" value={data.kpis.firings_7d} tone={data.kpis.firings_7d > 0 ? "amber" : "gray"} />
            <Kpi label="Fires 30d" value={data.kpis.firings_30d} tone={data.kpis.firings_30d > 0 ? "blue" : "gray"} />
          </div>
        )}
        <div className="mt-3 overflow-x-auto border-t pt-3">
          <div className="flex w-max min-w-full items-center gap-1 pb-1">
            {tabs.map((item) => (
              <button key={item.id} onClick={() => goTab(item.id)} className={`rounded-lg px-3 py-1.5 text-xs font-medium ${item.urgent ? (tab === item.id ? "bg-red-600 text-white ring-2 ring-red-200" : "bg-red-50 text-red-700 ring-1 ring-red-300 hover:bg-red-100") : tab === item.id ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>{item.label}{item.urgent && <span className="ml-1.5 inline-block h-2 w-2 animate-pulse rounded-full bg-current" title={`${summaryQ.data?.actionable_count ?? changesQ.data?.actionable_count ?? 0} changes require action`} />}{typeof item.count === "number" && <span className={`ml-1.5 rounded px-1.5 py-0.5 text-[10px] ${item.urgent ? (tab === item.id ? "bg-white/20" : "bg-red-100") : tab === item.id ? "bg-white/20" : "bg-gray-100"}`}>{item.count}</span>}</button>
            ))}
          </div>
        </div>
        {data?.report_exists && !managementTab && tab !== "overview" && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search rules, signals, resources…" className="w-64 rounded-lg border px-3 py-1.5 text-xs focus:border-brand focus:outline-none" />
            {tab === "rules" && <select value={status} onChange={(event) => setStatus(event.target.value)} className="rounded-lg border px-2 py-1.5 text-xs"><option value="all">All statuses</option><option value="overlap">Overlaps</option><option value="gap">Gaps</option><option value="ok">OK</option></select>}
            {tab === "rules" && <select aria-label="Sort rule analysis" value={ruleSort} onChange={(event) => { setRuleSort(event.target.value as typeof ruleSort); goPage(1); }} className="rounded-lg border px-2 py-1.5 text-xs"><option value="default">Default order</option><option value="cost_desc">Cost: highest first</option><option value="cost_asc">Cost: lowest first</option></select>}
            {(search || status !== "all" || ruleSort !== "default") && <button onClick={() => { setSearch(""); setStatus("all"); setRuleSort("default"); }} className="text-xs text-gray-500 hover:underline">Clear</button>}
          </div>
        )}
        <AnalysisProgress state={jobQ.data} compact />
      </header>

      <main ref={contentScrollRef} className="min-h-0 flex-1 overflow-auto px-6 py-4">
        {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {reportQ.isLoading ? <Skeleton rows={8} /> : reportQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(reportQ.error)}</div>
        ) : !ready ? (
          <div className="py-20 text-center text-sm text-gray-400">Select a workload, subscription, or management group to begin.</div>
        ) : !data?.report_exists && !managementTab ? (
          <div className="mx-auto mt-12 max-w-xl rounded-2xl border border-dashed bg-white p-10 text-center">
            <div className="text-4xl">🔔</div><h2 className="mt-3 text-base font-semibold text-gray-800">No alert analysis yet for this scope</h2><p className="mt-1 text-sm text-gray-500">Run a read-only scan of Azure Monitor rules, action groups, recipient paths, and AMBA baseline gaps.</p><button onClick={() => void refresh()} disabled={refreshing} className="mt-4 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">{refreshing ? "Analyzing…" : "Analyze alerts"}</button>
            <AnalysisProgress state={jobQ.data} />
          </div>
        ) : tab === "inbox" ? data?.demo ? (
          <div className="rounded-xl border bg-white p-10 text-center text-sm text-gray-500">Fired-alert state management is available only for live Azure scopes.</div>
        ) : firedQ.isLoading ? <Skeleton rows={8} /> : firedQ.isError ? <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(firedQ.error)}</div> : <PagedView rows={firedQ.data?.alerts ?? []} page={page} onPage={goPage}>{(pageRows) => <FiredAlertsTable rows={pageRows} canManage={!!capabilities?.can_manage_alert_state && !capabilities.read_only} busy={managementBusy} onState={(row, state) => void changeAlertState(row, state)} />}</PagedView>
          : tab === "manage-rules" ? data?.demo ? <div className="rounded-xl border bg-white p-10 text-center text-sm text-gray-500">Azure alert-rule management is unavailable for demo data.</div>
            : managedRulesQ.isLoading ? <Skeleton rows={8} />
              : managedRulesQ.isError ? <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(managedRulesQ.error)}</div>
                    : ruleActionGroupsQ.isLoading ? <Skeleton rows={8} />
                      : ruleActionGroupsQ.isError ? <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(ruleActionGroupsQ.error)}</div>
                      : <PagedView rows={managedRulesQ.data?.rules ?? []} page={page} onPage={goPage}>{(pageRows) => <ManagedAlertRulesTable rows={pageRows} caps={capabilities} busy={managementBusy} actionGroups={ruleActionGroupsQ.data?.action_groups ?? []} onCreate={createManagedRule} onEdit={(row) => void editManagedRule(row)} onClone={(row) => void cloneManagedRule(row)} onToggle={(row) => void toggleManagedRule(row)} onDelete={(row) => void deleteManagedRule(row)} onBulk={(rows, action, groupId) => void bulkManagedRules(rows, action, groupId)} />}</PagedView>
                  : tab === "action-groups" ? data?.demo ? <PagedView rows={data.action_groups} page={page} onPage={goPage}>{(pageRows) => <ActionGroupsTable rows={pageRows} />}</PagedView>
            : managedGroupsQ.isLoading ? <Skeleton rows={8} />
              : managedGroupsQ.isError ? <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(managedGroupsQ.error)}</div>
                : <PagedView rows={managedGroupsQ.data?.action_groups ?? []} page={page} onPage={goPage}>{(pageRows) => <ManagedActionGroupsTable rows={pageRows} caps={capabilities} busy={managementBusy} refreshing={managedGroupsQ.isFetching} onRefresh={() => void managedGroupsQ.refetch()} onCreate={() => { setError(""); setActionGroupEditorError(""); setEditor(blankActionGroup()); }} onEdit={(row) => void editActionGroup(row)} onClone={(row) => void cloneActionGroup(row)} onToggle={(row) => void toggleActionGroup(row)} onDelete={(row) => void deleteActionGroup(row)} onTest={(row) => void testActionGroup(row)} />}</PagedView>
          : tab === "deployment-plans" ? (
            <AlertBlueprintPlanner
              mode="plans"
              connectionId={connId}
              capabilities={capabilities}
              liveActionGroups={managedGroupsQ.data?.action_groups ?? []}
              workloads={workloads}
              selectedPlanId={focusedDeploymentPlanId}
            />
          )
          : tab === "changes" ? data?.demo ? <div className="rounded-xl border bg-white p-10 text-center text-sm text-gray-500">Managed Azure changes are unavailable for demo data.</div>
            : changesQ.isLoading ? <Skeleton rows={8} />
              : changesQ.isError ? <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(changesQ.error)}</div>
                : <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-white px-4 py-3 text-xs">
                      <span className="font-medium text-gray-600">Show:</span>
                      <div className="inline-flex overflow-hidden rounded-md border" role="group" aria-label="Filter managed changes">
                        {(["all", "action_required", "archived"] as const).map((value) => <button key={value} type="button" aria-pressed={changesView === value} onClick={() => { setChangesView(value); goPage(1); }} className={`px-3 py-1.5 transition ${changesView === value ? "bg-gray-900 font-medium text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{value === "all" ? "All" : value === "action_required" ? "Action Required" : "Archived"}{value === "action_required" && <span className={`ml-1.5 rounded px-1.5 py-0.5 text-[10px] ${changesView === value ? "bg-white/20" : "bg-red-50 text-red-700"}`}>{changesQ.data?.actionable_count ?? 0}</span>}</button>)}
                      </div>
                      <label className="ml-auto flex items-center gap-2 text-gray-600">Sort by<select aria-label="Sort managed changes" value={changesSort} onChange={(event) => { setChangesSort(event.target.value as ChangesSort); goPage(1); }} className="rounded-md border px-2.5 py-1.5 text-xs text-gray-700"><option value="newest">Newest requested</option><option value="oldest">Oldest requested</option><option value="status">Status</option><option value="risk">Risk</option><option value="change">Change</option></select></label>
                      <span className="text-[10px] text-gray-400">{CHANGES_PAGE_SIZE} per page</span>
                    </div>
                    <ChangesTable rows={changesQ.data?.changes ?? []} caps={capabilities} busy={managementBusy} applyingIds={applyingChangeIds} preparingCount={bulkPreparingCount} onDecision={(row, decision) => void decideManagedChange(row, decision)} onApply={(row) => void applyManagedChange(row)} onBulkDecision={bulkDecideManagedChanges} onBulkApply={bulkApplyManagedChanges} onRollback={(row) => void rollbackManagedChange(row)} />
                    <PageBar total={changesQ.data?.total ?? 0} page={page} pageSize={CHANGES_PAGE_SIZE} onPage={goPage} />
                  </div>
          : !data ? <div className="py-20 text-center text-sm text-gray-400">No analysis snapshot is available.</div>
          : data.error && !data.rules.length ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">{data.error}</div>
        ) : tab === "overview" ? <Overview overlaps={data.active_overlaps ?? data.overlaps} gaps={data.active_gaps ?? data.gaps} rules={data.rules} costSummary={data.cost_summary} activityLogCoverage={data.demo ? <section className="rounded-xl border bg-white p-4 text-xs text-gray-500">Essential Activity Log coverage is available for live Azure scopes.</section> : <ActivityLogCoverageSection coverage={activityLogCoverageQ.data?.coverage} loading={activityLogCoverageQ.isLoading} error={activityLogCoverageQ.error} disabled={!capabilities?.can_manage_rules || !!capabilities.read_only} onOpen={() => setActivityLogWizardOpen(true)} />} />
          : tab === "overlaps" ? <PagedView rows={overlaps} page={page} onPage={goPage}>{(pageRows) => <OverlapsTable rows={pageRows} onDismiss={(id) => void recordDecision("overlap", id, "dismiss_finding")} />}</PagedView>
          : tab === "gaps" ? <PagedView rows={gaps} page={page} onPage={goPage}>{(pageRows) => <GapsTable rows={pageRows} selectedRows={selectedGaps} selectedIds={selectedGapIds} plansByGap={gapPlansQ.data?.by_gap ?? {}} canPlan={canPlanGaps} onSelectionChange={setSelectedGapIds} onCreatePlan={() => setGapPlannerOpen(true)} onOpenPlan={(planId) => { setFocusedDeploymentPlanId(planId); goTab("deployment-plans"); }} onExempt={(row, index) => void recordDecision("gap", gapIdentity(row, index), "dismiss_finding")} onCreateRule={capabilitiesQ.data?.can_manage_rules && !capabilitiesQ.data.read_only ? (row) => setRuleEditor(ruleFromGap(row, scopeKind === "subscription" ? subId : "")) : undefined} />}</PagedView>
          : tab === "rules" ? <PagedView rows={rules} page={page} onPage={goPage}>{(pageRows) => <RulesTable rows={pageRows} onDecision={(rule, action) => void recordDecision("rule", rule.id, action)} />}</PagedView>
          : tab === "visualize" ? <NotificationSimulatorPanel params={managementParams} />
          : null}
      </main>
      {editor && <ActionGroupEditor initial={editor} connectionId={connId} busy={managementBusy === "save"} saveError={actionGroupEditorError} onClose={() => { setActionGroupEditorError(""); setEditor(null); }} onSave={(value, reason) => void saveActionGroup(value, reason)} />}
      {ruleEditor && <AlertRuleEditor initial={ruleEditor} connectionId={connId} workloadId={scopeKind === "workload" ? effectiveWorkloadId : ""} actionGroups={ruleActionGroupsQ.data?.action_groups ?? []} canPreview={!!capabilitiesQ.data?.can_preview_queries} busy={managementBusy === "rule-save"} onClose={() => setRuleEditor(null)} onSave={saveManagedRule} />}
      {gapPlannerOpen && selectedGaps.length > 0 && <GapRemediationPlanner gaps={selectedGaps} scopeParams={managementParams} liveActionGroups={managedGroupsQ.data?.action_groups ?? []} capabilities={capabilitiesQ.data} onClose={() => setGapPlannerOpen(false)} onOpenPlan={(planId) => { setGapPlannerOpen(false); setFocusedDeploymentPlanId(planId); goTab("deployment-plans"); }} onSubmitted={(plan) => { setGapPlannerOpen(false); setSelectedGapIds(new Set()); setFocusedDeploymentPlanId(plan.id); goTab("deployment-plans"); void Promise.all([qc.invalidateQueries({ queryKey: ["alerts-manager-deployment-plans"] }), qc.invalidateQueries({ queryKey: ["alerts-manager-deployment-plans-by-gap"] }), invalidateManagedChanges()]); }} />}
      {activityLogWizardOpen && activityLogCoverageQ.data && <ActivityLogSetupWizard coverage={activityLogCoverageQ.data.coverage} scopeParams={managementParams} actionGroups={managedGroupsQ.data?.action_groups ?? []} capabilities={capabilities} onClose={() => setActivityLogWizardOpen(false)} onOpenDiagnostics={() => { setActivityLogWizardOpen(false); setActivityLogDiagnosticsOpen(true); }} onSubmitted={() => { setActivityLogWizardOpen(false); goTab("changes"); requestAnimationFrame(() => contentScrollRef.current?.scrollTo({ top: 0, behavior: "auto" })); void Promise.all([invalidateManagedChanges(), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.activityLogCoverageRoot }), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.rulesRoot })]); }} />}
      {activityLogDiagnosticsOpen && <ActivityLogDiagnosticsWizard scopeParams={managementParams} capabilities={capabilities} onBack={() => { setActivityLogDiagnosticsOpen(false); setActivityLogWizardOpen(true); }} onClose={() => setActivityLogDiagnosticsOpen(false)} onSubmitted={() => { setActivityLogDiagnosticsOpen(false); goTab("changes"); requestAnimationFrame(() => contentScrollRef.current?.scrollTo({ top: 0, behavior: "auto" })); void Promise.all([invalidateManagedChanges(), qc.invalidateQueries({ queryKey: queryKeys.alertsManager.activityLogCoverageRoot })]); }} />}
    </div>
  );
}
