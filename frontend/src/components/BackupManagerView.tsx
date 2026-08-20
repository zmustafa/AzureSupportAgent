/**
 * Backup Manager — the operational management plane for Azure Backup and Site Recovery.
 *
 * Sibling of Alerts Manager (fired alerts → managed changes); here the inbox is failed backup
 * jobs, the rules are backup policies, the destinations are vaults, and the gap queue comes
 * from unprotected resources plus Backup & DR Coverage findings.
 *
 * Two capabilities are deliberately absent and shown as such rather than hidden: Backup
 * Manager never restores data, and never performs destructive backup operations.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  downloadBlob,
  type BackupChangesResp,
  type BackupChronicFailure,
  type BackupFailureCluster,
  type BackupGap,
  type BackupInstance,
  type BackupJob,
  type BackupManagerChange,
  type BackupManagerScope,
  type BackupPolicy,
  type BackupRefreshJobResponse,
  type BackupRemediationItem,
  type BackupReplicationItem,
  type BackupSnapshot,
  type BackupVaultScore,
} from "../api";
import { queryKeys } from "../queryKeys";
import { azurePortalResourceUrl, azurePortalSubscriptionUrl } from "../utils/azurePortal";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { BackupFlowTab } from "./backup/BackupFlowTab";
import { BackupManagerCleanup } from "./backup/BackupManagerCleanup";
import { BackupManagerFleet } from "./backup/BackupManagerFleet";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { ManagementGroupPicker } from "./ManagementGroupPicker";

type Tab =
  | "overview" | "flow" | "inventory" | "jobs" | "policies" | "vaults"
  | "gaps" | "dr" | "cost" | "changes";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "🛡️" },
  { id: "flow", label: "Protection flow", icon: "🔀" },
  { id: "inventory", label: "Protection", icon: "📦" },
  { id: "jobs", label: "Job inbox", icon: "📥" },
  { id: "policies", label: "Policies", icon: "📜" },
  { id: "vaults", label: "Vaults", icon: "🔐" },
  { id: "gaps", label: "Gaps", icon: "🕳️" },
  { id: "dr", label: "DR & drills", icon: "🚨" },
  { id: "cost", label: "Cost & waste", icon: "💰" },
  { id: "changes", label: "Managed changes", icon: "📋" },
];
const VALID_TABS = new Set<string>(TABS.map((t) => t.id));

/**
 * Estate-wide views that sit ABOVE the per-scope module, mirroring Backup & DR Coverage and
 * Change Explorer. One scope's tabs and the whole fleet are different altitudes, so they are
 * not peers in the same tab row.
 */
type MainView = "manager" | "fleet" | "cleanup";
const MAIN_VIEWS: { id: MainView; label: string }[] = [
  { id: "manager", label: "💾 Manager" },
  { id: "fleet", label: "🚀 Fleet" },
  { id: "cleanup", label: "🧹 Cleanup" },
];

function MainViewTabs({ value, onChange }: { value: MainView; onChange: (value: MainView) => void }) {
  return (
    <div className="flex items-center gap-1 border-b bg-white px-5 pt-2">
      {MAIN_VIEWS.map((view) => (
        <button
          key={view.id}
          onClick={() => onChange(view.id)}
          aria-current={value === view.id ? "page" : undefined}
          className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === view.id
            ? "border-brand font-medium text-brand"
            : "border-transparent text-gray-500 hover:text-gray-700"}`}
        >
          {view.label}
        </button>
      ))}
    </div>
  );
}

const BAND_STYLE: Record<string, string> = {
  green: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  amber: "bg-amber-50 text-amber-700 ring-amber-200",
  red: "bg-rose-50 text-rose-700 ring-rose-200",
};
const STATUS_STYLE: Record<string, string> = {
  pass: "bg-emerald-50 text-emerald-700",
  warn: "bg-amber-50 text-amber-700",
  fail: "bg-rose-50 text-rose-700",
  na: "bg-gray-100 text-gray-500",
  succeeded: "bg-emerald-50 text-emerald-700",
  failed: "bg-rose-50 text-rose-700",
  running: "bg-sky-50 text-sky-700",
  unknown: "bg-gray-100 text-gray-600",
};
const CHANGE_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-50 text-amber-700",
  approved: "bg-indigo-50 text-indigo-700",
  applying: "bg-sky-50 text-sky-700",
  applied: "bg-emerald-50 text-emerald-700",
  failed: "bg-rose-50 text-rose-700",
  rejected: "bg-gray-100 text-gray-600",
  rolled_back: "bg-gray-100 text-gray-600",
};

function fmtAge(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "—";
  if (hours < 1) return "<1h";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}
function fmtMoney(value: number, currency: string): string {
  const code = (currency || "USD").toUpperCase();
  // Small figures need their cents (a EUR 2.50 protected-instance charge is not "3"); large
  // totals read better whole. Intl carries the right symbol for whatever the tenant bills in.
  const digits = Math.abs(value) < 1000 ? 2 : 0;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency: code,
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return `${code} ${value.toFixed(digits)}`;
  }
}

/** Cost Management returns daily buckets as `20260601`. */
function fmtUsageDate(value: string): string {
  const text = String(value ?? "");
  return /^\d{8}$/.test(text) ? `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6)}` : text;
}
function fmtDate(value: string): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function Stat({ label, value, hint, band }: { label: string; value: string | number; hint?: string; band?: string }) {
  return (
    <div className={`rounded-xl border p-3 ${band ? `ring-1 ${BAND_STYLE[band] ?? ""}` : "bg-white"}`}>
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-0.5 text-2xl font-semibold text-gray-900">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-gray-500">{hint}</div>}
    </div>
  );
}

function Pill({ tone, children }: { tone: string; children: React.ReactNode }) {
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${tone}`}>{children}</span>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">{children}</div>;
}

type SortDirection = "asc" | "desc";
type TableSort<K extends string> = { key: K; direction: SortDirection };

function useTableSort<K extends string>(storageKey: string, fallback: TableSort<K>, allowed: readonly K[]) {
  const [sort, setSort] = useState<TableSort<K>>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null") as TableSort<K> | null;
      if (saved && allowed.includes(saved.key) && (saved.direction === "asc" || saved.direction === "desc")) return saved;
    } catch { /* a stale preference is not a page failure */ }
    return fallback;
  });
  const update = (key: K, first: SortDirection = "asc") => {
    setSort((current) => {
      const next = current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" as const : "asc" as const }
        : { key, direction: first };
      localStorage.setItem(storageKey, JSON.stringify(next));
      return next;
    });
  };
  const replace = (next: TableSort<K>) => {
    localStorage.setItem(storageKey, JSON.stringify(next));
    setSort(next);
  };
  return { sort, update, replace };
}

function compareText(left: unknown, right: unknown): number {
  const a = String(left ?? "").trim();
  const b = String(right ?? "").trim();
  if (!a && b) return 1;
  if (a && !b) return -1;
  return a.localeCompare(b, undefined, { sensitivity: "base", numeric: true });
}

function compareNumber(left: unknown, right: unknown): number {
  const a = typeof left === "number" && Number.isFinite(left) ? left : null;
  const b = typeof right === "number" && Number.isFinite(right) ? right : null;
  if (a === null && b !== null) return 1;
  if (a !== null && b === null) return -1;
  return (a ?? 0) - (b ?? 0);
}

function compareTextDirection(left: unknown, right: unknown, direction: SortDirection): number {
  const a = String(left ?? "").trim();
  const b = String(right ?? "").trim();
  if (!a && b) return 1;
  if (a && !b) return -1;
  const result = compareText(a, b);
  return direction === "asc" ? result : -result;
}

function compareNumberDirection(left: unknown, right: unknown, direction: SortDirection): number {
  const a = typeof left === "number" && Number.isFinite(left) ? left : null;
  const b = typeof right === "number" && Number.isFinite(right) ? right : null;
  if (a === null && b !== null) return 1;
  if (a !== null && b === null) return -1;
  const result = compareNumber(a, b);
  return direction === "asc" ? result : -result;
}

function SortableTh<K extends string>({
  label, column, state, onSort, first = "asc", directionLabels, className = "",
}: {
  label: string; column: K; state: TableSort<K>; onSort: (key: K, first?: SortDirection) => void;
  first?: SortDirection; directionLabels?: Record<SortDirection, string>; className?: string;
}) {
  const active = state.key === column;
  const directionLabel = active ? directionLabels?.[state.direction] : undefined;
  return (
    <th className={className} aria-sort={active ? (state.direction === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        onClick={() => onSort(column, first)}
        className="inline-flex w-full items-center gap-1 rounded text-left hover:text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand/40"
        aria-label={`Sort by ${label.toLowerCase()}${directionLabel ? `; currently ${directionLabel}` : ""}`}
        title={directionLabel}
      >
        <span>{label}</span>
        <span aria-hidden="true" className={active ? "text-brand" : "text-gray-300"}>
          {active ? (state.direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function PortalLink({ resourceId, portalHost, label, subscriptionId, className = "" }: {
  resourceId?: string | null; portalHost?: string; label: string; subscriptionId?: string | null; className?: string;
}) {
  const url = subscriptionId
    ? azurePortalSubscriptionUrl(subscriptionId, portalHost)
    : azurePortalResourceUrl(resourceId, portalHost);
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => event.stopPropagation()}
      className={`inline-flex items-center gap-1 text-brand hover:underline ${className}`}
      aria-label={`${label} in Azure portal`}
      title={`${label} in Azure portal`}
    >
      ↗<span className="sr-only">{label}</span>
    </a>
  );
}

function ErrorBanner({ errors }: { errors: Record<string, string> | undefined }) {
  const entries = Object.entries(errors ?? {}).filter(([, v]) => v);
  if (!entries.length) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
      <div className="font-medium">Some backup data could not be collected — the view below is incomplete.</div>
      <ul className="mt-1 list-disc pl-4">
        {entries.map(([source, message]) => (
          <li key={source}><span className="font-mono">{source}</span>: {message}</li>
        ))}
      </ul>
    </div>
  );
}

function elapsedText(startedAt?: string | null, finishedAt?: string | null): string {
  if (!startedAt) return "0s";
  const end = finishedAt ? Date.parse(finishedAt) : Date.now();
  const seconds = Math.max(0, Math.floor((end - Date.parse(startedAt)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

const PHASE_LABEL: Record<string, string> = {
  start: "Starting",
  scope: "Resolving scope",
  query: "Reading Resource Graph",
  orphans: "Checking for orphans",
  vaults: "Reading vault configuration",
  analyze: "Analyzing",
  cost: "Pricing",
  save: "Saving",
  done: "Complete",
  error: "Failed",
};

/**
 * Live progress for a running analysis.
 *
 * The sweep takes tens of seconds against a real estate, so a bare spinner leaves the
 * operator unable to tell a slow subscription from a hung one. Each line names the source and
 * the row count it returned, and the job keeps running server-side if they navigate away.
 */
function AnalysisProgress({ state }: { state?: BackupRefreshJobResponse }) {
  const [, tick] = useState(0);
  const [dismissedJobId, setDismissedJobId] = useState("");
  const job = state?.job;
  useEffect(() => {
    if (job?.status !== "running") return;
    const timer = window.setInterval(() => tick((v) => v + 1), 1000);
    return () => window.clearInterval(timer);
  }, [job?.status]);
  useEffect(() => {
    if (!job || job.status !== "done") return;
    const timer = window.setTimeout(() => setDismissedJobId(job.id), 4000);
    return () => window.clearTimeout(timer);
  }, [job?.id, job?.status]);
  if (!job || dismissedJobId === job.id) return null;
  // A finished run is worth confirming, but only while it is still news. Without this the
  // completion banner replays on every page load until the next analysis.
  const finishedAgo = job.finished_at ? Date.now() - Date.parse(job.finished_at) : 0;
  if (job.status === "done" && finishedAgo > 60_000) return null;

  const lines = state?.progress ?? [];
  const last = lines.at(-1);
  const current = last?.message || job.last_message || "Preparing the backup estate analysis…";
  const phase = last?.phase || (job.status === "running" ? "start" : job.status);
  const running = job.status === "running";
  const failed = job.status === "error";
  const phases = Object.keys(PHASE_LABEL).filter((p) => p !== "error");
  const reached = phases.indexOf(phase === "error" ? "done" : phase);
  const pct = failed ? 100 : Math.round(((reached + 1) / phases.length) * 100);

  return (
    <section role="status" aria-live="polite"
      className={`overflow-hidden rounded-xl border ${failed ? "border-rose-200 bg-rose-50" : "border-sky-200 bg-sky-50/70"}`}>
      <div className="flex items-start gap-3 px-4 py-3">
        <span className={`mt-1 h-2.5 w-2.5 flex-none rounded-full ${
          running ? "animate-pulse bg-sky-500" : failed ? "bg-rose-500" : "bg-emerald-500"}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-gray-900">{PHASE_LABEL[phase] ?? phase}</span>
            <span className="text-[10px] tabular-nums text-gray-500">
              Elapsed {elapsedText(job.started_at, job.finished_at)}
            </span>
            {running && <span className="rounded bg-sky-100 px-2 py-0.5 text-[10px] font-medium text-sky-700">Running on server</span>}
            <span className="text-[10px] tabular-nums text-gray-500">{lines.length} step(s)</span>
          </div>
          <p className="mt-0.5 text-xs text-gray-700">{failed ? job.error || current : current}</p>
          {running && (
            <p className="mt-1 text-[10px] text-gray-500">
              This analysis continues on the server if you switch tabs or close the page. Come back to reconnect to it.
            </p>
          )}
        </div>
      </div>
      <div className="h-1 bg-sky-100">
        <div className={`h-full transition-all duration-500 ${failed ? "bg-rose-500" : "bg-sky-500"}`}
          style={{ width: `${pct}%` }} />
      </div>
      {lines.length > 0 && (
        <ol className="max-h-56 space-y-1 overflow-auto border-t border-sky-100 bg-white/70 px-4 py-2">
          {lines.map((line) => (
            <li key={line.seq} className="flex gap-2 text-[11px] leading-5 text-gray-600">
              <span className={line.level === "error" ? "text-rose-500" : line.phase === "done" ? "text-emerald-500" : "text-sky-500"}>
                {line.level === "error" ? "!" : "✓"}
              </span>
              <span className="text-gray-400">{PHASE_LABEL[line.phase] ?? line.phase}</span>
              <span className="min-w-0 flex-1">{line.message}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** Shown on every tab until this scope has been analyzed at least once. */
function NeedsAnalysis({ onAnalyze, analyzing, startedAt }: {
  onAnalyze: () => void; analyzing: boolean; startedAt?: string | null;
}) {
  return (
    <div className="rounded-xl border border-dashed bg-white p-10 text-center">
      <div className="text-4xl">💾</div>
      <h2 className="mt-3 text-base font-semibold text-gray-800">No backup analysis yet for this scope</h2>
      <p className="mx-auto mt-1 max-w-xl text-sm text-gray-500">
        Backup Manager reads the whole estate once — vaults, protected items, policies, jobs, Site Recovery,
        and real cost — and then serves every tab from that single analysis. Nothing is fetched automatically,
        so the numbers never move while you are working a decision.
      </p>
      <button onClick={onAnalyze} disabled={analyzing}
        className="mt-4 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium tabular-nums text-white disabled:opacity-50">
        {analyzing ? `Analyzing… ${elapsedText(startedAt)}` : "Analyze backups"}
      </button>
    </div>
  );
}

export function BackupManagerPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { tab: routeTab } = useParams<{ tab?: string }>();
  const [routeSearch] = useSearchParams();
  // The estate-wide views share the module's `:tab` segment so every view is linkable and
  // survives a reload, but they are a different altitude from the per-scope tabs below.
  const mainView: MainView = routeTab === "fleet" ? "fleet" : routeTab === "cleanup" ? "cleanup" : "manager";
  const tab: Tab = routeTab && VALID_TABS.has(routeTab) ? (routeTab as Tab) : "overview";

  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription" | "management_group">("azsup.backupManager.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.backupManager.workloadId", "");
  useWorkloadDeepLink(setScopeKind as (k: "workload" | "subscription") => void, setWorkloadId);
  const [subId, setSubId] = usePersistedState("azsup.backupManager.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.backupManager.subName", "");
  const [mgId, setMgId] = usePersistedState("azsup.backupManager.mgId", "");
  const [mgName, setMgName] = usePersistedState("azsup.backupManager.mgName", "");
  const [mgRefreshToken, setMgRefreshToken] = useState(0);
  const [connId, setConnId] = usePersistedState("azsup.backupManager.connectionId", "");
  const [banner, setBanner] = useState("");
  const [exportingWorkbook, setExportingWorkbook] = useState(false);
  // Set when the operator clicks through from the protection flow, so the destination tab
  // opens on what they were looking at instead of making them find it again.
  const [focusGapIds, setFocusGapIds] = useState<string[]>([]);
  const [focusVaultId, setFocusVaultId] = useState("");

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, staleTime: 60_000 });
  const workloads = workloadsQ.data?.workloads ?? [];
  const selectedWorkload = workloads.find((workload) => workload.id === workloadId);
  const workloadConnectionId = String(selectedWorkload?.connection_id ?? "");
  const effectiveConnId = scopeKind === "workload"
    ? workloadConnectionId || (workloadsQ.isSuccess ? connId : "")
    : connId;

  const scope: BackupManagerScope = useMemo(
    () => ({
      connection_id: effectiveConnId,
      workload_id: scopeKind === "workload" ? workloadId : "",
      subscription_id: scopeKind === "subscription" ? subId : "",
      management_group_id: scopeKind === "management_group" ? mgId : "",
    }),
    [effectiveConnId, scopeKind, workloadId, subId, mgId],
  );
  const scopeReady = scopeKind === "workload"
    ? !!workloadId && workloadsQ.isSuccess && !!selectedWorkload
    : scopeKind === "subscription" ? !!subId : !!mgId;

  useEffect(() => {
    const linkedWorkload = routeSearch.get("workload_id");
    const linkedMg = routeSearch.get("management_group_id");
    const linkedConnection = routeSearch.get("connection_id");
    if (linkedConnection && linkedConnection !== connId) setConnId(linkedConnection);
    if (linkedWorkload) {
      setMgId(""); setMgName("");
      setSubId(""); setSubName("");
    } else if (linkedMg) {
      setScopeKind("management_group");
      setWorkloadId("");
      setSubId(""); setSubName("");
      setMgId(linkedMg);
      setMgName(linkedMg);
      setMgRefreshToken((value) => value + 1);
    }
  }, [connId, routeSearch, setConnId, setMgId, setMgName, setScopeKind, setSubId, setSubName, setWorkloadId]);

  useEffect(() => {
    if (scopeKind !== "workload" || !workloadId || !workloadsQ.data) return;
    if (!selectedWorkload) {
      setWorkloadId("");
      return;
    }
    if (workloadConnectionId && workloadConnectionId !== connId) {
      setConnId(workloadConnectionId);
      setBanner(`Switched Azure connection to match ${selectedWorkload.name}.`);
    }
  }, [connId, scopeKind, selectedWorkload, setConnId, setWorkloadId, workloadConnectionId, workloadId, workloadsQ.data]);

  const capsQ = useQuery({
    queryKey: queryKeys.backupManager.capabilities(scope),
    queryFn: () => api.backupManagerCapabilities(scope),
    enabled: scopeReady,
    retry: false,
  });
  const caps = capsQ.data;
  const portalHost = caps?.portal_host ?? "";

  // The single analysis every tab reads. Never refetches on its own — the never-stale
  // defaults live in queryClient.ts so this guarantee cannot be lost at a call site.
  const snapshotKey = queryKeys.backupManager.snapshot(scope);
  const snapshotQ = useQuery({
    queryKey: snapshotKey,
    queryFn: () => api.backupManagerSnapshot(scope),
    enabled: scopeReady,
  });
  const snapshot = snapshotQ.data;
  const analyzed = !!snapshot?.report_exists;

  // A job THIS browser started, remembered locally until the server confirms it finished.
  //
  // Without it, "is an analysis running?" hangs entirely on the newest poll response, so a
  // single reply that doesn't report the job — a poll that raced the start, a backend whose
  // in-memory registry was restarted, another replica behind the ingress — flips the button
  // back to "Analyze backups" AND stops the polling that would have recovered it. The sweep
  // keeps running server-side and still writes its snapshot, so the operator sees a moment of
  // "Analyzing…", then an idle screen, while the work is very much in flight.
  const [pendingJob, setPendingJob] = useState<{ scope: string; id: string; startedAt: string } | null>(null);
  const scopeSig = JSON.stringify(queryKeys.backupManager.analyzeJob(scope));
  const pending = pendingJob && pendingJob.scope === scopeSig ? pendingJob : null;
  // refetchInterval is evaluated by the query cache outside the render closure, so the flag it
  // reads has to live in a ref.
  const pendingRef = useRef(false);
  useEffect(() => { pendingRef.current = !!pending; }, [pending]);

  // The job query is the opposite: always current, and polls only while work is in flight.
  // Mounting it on every tab is what lets an operator start an analysis, navigate away, and
  // come back to a live progress log instead of an apparently idle screen.
  const jobQ = useQuery({
    queryKey: queryKeys.backupManager.analyzeJob(scope),
    queryFn: () => api.backupManagerAnalyzeJob(scope),
    enabled: scopeReady && !caps?.demo,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: (query) =>
      query.state.data?.job?.status === "running" || pendingRef.current ? 1000 : false,
  });
  const job = jobQ.data?.job;
  const serverRunning = job?.status === "running";

  const changesQ = useQuery({
    queryKey: queryKeys.backupManager.changes(effectiveConnId, 1, 100, "all", ""),
    queryFn: () => api.backupManagerChanges(effectiveConnId, 1, 100, "all", ""),
    enabled: scopeReady && !caps?.demo,
    refetchInterval: (query) =>
      (query.state.data?.applying_count ?? 0) > 0 ? 8_000 : false,
  });
  const actionable = changesQ.data?.actionable_count ?? 0;
  const applying = changesQ.data?.applying_count ?? 0;

  // Hand a finished analysis straight to the tabs. Writing the result into the cache rather
  // than invalidating avoids a second round-trip for a payload we already hold, and keeps the
  // snapshot query from ever having to refetch.
  const handledJobs = useRef(new Set<string>());
  useEffect(() => {
    const state = jobQ.data;
    if (!state?.job || state.job.status === "running") return;
    const identity = `${JSON.stringify(snapshotKey)}:${state.job.id}`;
    if (handledJobs.current.has(identity)) return;
    handledJobs.current.add(identity);
    if (state.job.status === "done" && state.result) {
      qc.setQueryData(snapshotKey, state.result);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
      setBanner("Backup analysis complete.");
    } else if (state.job.status === "error") {
      setBanner(state.job.error || "Backup analysis failed.");
    }
    // snapshotKey is derived from scope, which is already a dependency via its members.
  }, [jobQ.data, qc, snapshotKey, setBanner]);

  function goTab(next: Tab) {
    navigate(`/backup-manager/${next}`);
  }

  const analyzeM = useMutation({
    mutationFn: () => api.backupManagerAnalyzeStart(scope),
    onSuccess: (state) => {
      qc.setQueryData(queryKeys.backupManager.analyzeJob(scope), state);
      if (state.job) {
        setPendingJob({
          scope: scopeSig,
          id: state.job.id,
          startedAt: state.job.started_at || new Date().toISOString(),
        });
      }
      void jobQ.refetch();
    },
    onError: (e: Error) => setBanner(e.message),
  });
  const startAnalysis = () => analyzeM.mutate();

  async function exportWorkbook() {
    if (!analyzed || exportingWorkbook) return;
    setExportingWorkbook(true);
    try {
      const blob = await api.backupManagerWorkbook(scope);
      const date = new Date().toISOString().slice(0, 10);
      downloadBlob(blob, `backup-manager-review-${date}.xlsx`);
      setBanner("Excel review pack downloaded from the last completed analysis.");
    } catch (error) {
      setBanner(error instanceof Error ? error.message : "Could not prepare the Excel review pack.");
    } finally {
      setExportingWorkbook(false);
    }
  }

  // Stop tracking a locally-started job once the server has actually reported it (or a newer
  // run) as finished. A poll that returns an OLDER finished job — the previous analysis for
  // this scope — must not clear it, or we are back to the bug this marker exists to prevent.
  useEffect(() => {
    if (!pending) return;
    const settled =
      job &&
      job.status !== "running" &&
      (job.id === pending.id || Date.parse(job.started_at || "") >= Date.parse(pending.startedAt));
    if (settled) {
      setPendingJob(null);
      return;
    }
    // The registry no longer knows about it at all. Give the start a grace window (a poll can
    // legitimately race it), then stop claiming an analysis is in flight rather than spinning
    // forever on a job nobody can see.
    if (!job && Date.now() - Date.parse(pending.startedAt) > 30_000) {
      setPendingJob(null);
      setBanner("Lost contact with the analysis job — it may still be finishing on the server. Reopen Backup Manager to reconnect.");
    }
  }, [job, pending, jobQ.dataUpdatedAt]);

  // What the operator is told. The local marker covers the window where the server has not
  // (yet) reported the job, and isPending covers the click-to-response gap.
  const analyzing = analyzeM.isPending || serverRunning || !!pending;
  const analyzingSince = job?.started_at ?? pending?.startedAt;
  // Show live progress from the moment work starts, even before the first poll lands.
  const progressState: BackupRefreshJobResponse | undefined = jobQ.data?.job
    ? jobQ.data
    : pending
      ? {
          job: {
            id: pending.id, key: "", status: "running", started_at: pending.startedAt,
            finished_at: null, progress_count: 0, last_message: "", error: "",
          },
          progress: [],
          result: null,
        }
      : undefined;

  useEffect(() => {
    if (!banner) return;
    const timer = setTimeout(() => setBanner(""), 6000);
    return () => clearTimeout(timer);
  }, [banner]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <MainViewTabs value={mainView} onChange={(next) => navigate(`/backup-manager/${next === "manager" ? "overview" : next}`)} />
      {mainView === "fleet" ? (
        <BackupManagerFleet onOpenWorkload={(id, connectionId) => {
          setScopeKind("workload");
          setWorkloadId(id);
          if (connectionId) setConnId(connectionId);
          goTab("overview");
        }} />
      ) : mainView === "cleanup" ? (
        <BackupManagerCleanup canPurge={!!caps?.can_approve} />
      ) : (
      <>
      <header className="border-b bg-white px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              <span>💾</span> Backup Manager
            </h1>
            <p className="text-xs text-gray-500">
              Backup and DR oversight with approval-gated changes—no restores or destructive operations.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ConnectionScopePicker value={effectiveConnId} onChange={(id) => {
              if (id === connId) return;
              setConnId(id);
              setWorkloadId("");
              setSubId(""); setSubName("");
              setMgId(""); setMgName("");
            }} align="right" />
            <div className="flex max-w-full flex-wrap items-center gap-2">
              <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
                {(["workload", "subscription", "management_group"] as const).map((kind) => (
                  <button
                    key={kind}
                    type="button"
                    aria-pressed={scopeKind === kind}
                    onClick={() => {
                      setScopeKind(kind);
                      if (kind === "management_group") setMgRefreshToken((value) => value + 1);
                    }}
                    className={`rounded-md px-2.5 py-1 ${scopeKind === kind ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}
                  >
                    {kind === "workload" ? "Workload" : kind === "subscription" ? "Subscription" : "Management group"}
                  </button>
                ))}
              </div>
              {scopeKind === "management_group" ? (
                <ManagementGroupPicker
                  value={mgId}
                  valueName={mgName}
                  connectionId={connId}
                  refreshToken={mgRefreshToken}
                  onPick={(id, name) => { setMgId(id); setMgName(name); }}
                />
              ) : (
                <ScopePicker
                  scopeKind={scopeKind}
                  onScopeKindChange={() => {}}
                  workloads={workloads}
                  workloadId={workloadId}
                  onWorkloadChange={(id) => {
                    const workload = workloads.find((item) => item.id === id);
                    if (workload?.connection_id) setConnId(workload.connection_id);
                    setWorkloadId(id);
                  }}
                  subId={subId}
                  subName={subName}
                  onSubPick={(id, name) => { setSubId(id); setSubName(name); }}
                  workloadPlaceholder="Select a workload…"
                  connectionId={connId}
                  hideKindToggle
                />
              )}
            </div>
            {scopeKind === "subscription" && (
              <PortalLink subscriptionId={subId} portalHost={portalHost} label="Open selected subscription" className="rounded border px-2 py-1 text-xs" />
            )}
            {scopeKind === "management_group" && analyzed && (
              <span className="text-[11px] text-gray-500" title={snapshot?.scope.scope_id}>
                {snapshot?.scope.scope_name || mgName || mgId} · {snapshot?.scope.subscription_count ?? snapshot?.scope.subscriptions.length ?? 0} subscriptions
              </span>
            )}
            {snapshot?.generated_at && !caps?.demo && (
              <span className="text-[11px] text-gray-500" title={fmtDate(snapshot.generated_at)}>
                Analyzed {fmtDate(snapshot.generated_at)}
              </span>
            )}
            {tab === "overview" && (
              <button
                data-testid="backup-manager-workbook"
                type="button"
                onClick={() => void exportWorkbook()}
                disabled={!analyzed || exportingWorkbook}
                title={analyzed
                  ? "Download every Backup Manager section from the last completed analysis."
                  : "Analyze backups before exporting a review pack."}
                className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-800 hover:bg-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {exportingWorkbook ? "Preparing workbook…" : "⬇ Excel review pack"}
              </button>
            )}
            {!caps?.demo && (
              <button
                onClick={startAnalysis}
                disabled={!scopeReady || analyzing || analyzeM.isPending}
                title="Backup Manager never re-reads Azure on its own. Everything you see is from the last analysis."
                className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium tabular-nums text-white hover:bg-gray-700 disabled:opacity-50"
              >
                {analyzing
                  ? `Analyzing… ${elapsedText(analyzingSince)}`
                  : analyzed ? "↻ Analyze again" : "Analyze backups"}
              </button>
            )}
          </div>
        </div>
        <nav aria-label="Backup Manager sections" className="mt-3 flex flex-wrap gap-1">
          {TABS.map((t) => {
            const isActive = tab === t.id;
            const urgent = t.id === "changes" && actionable > 0;
            return (
              <button
                key={t.id}
                onClick={() => goTab(t.id)}
                className={[
                  "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm",
                  isActive
                    ? urgent ? "bg-rose-600 font-medium text-white" : "bg-brand-dark font-medium text-white"
                    : urgent ? "text-rose-700 ring-1 ring-rose-300 hover:bg-rose-50" : "text-gray-600 hover:bg-gray-100",
                ].join(" ")}
              >
                <span>{t.icon}</span>
                {t.label}
                {urgent && (
                  <span className="ml-1 inline-flex items-center gap-1">
                    <span className={`h-1.5 w-1.5 rounded-full ${isActive ? "bg-white" : "bg-rose-500"} animate-pulse`} />
                    <span className="text-[11px] font-semibold">{actionable}</span>
                  </span>
                )}
                {t.id === "changes" && applying > 0 && (
                  <span className="ml-1 text-[11px] text-sky-600">{applying} applying</span>
                )}
              </button>
            );
          })}
        </nav>
      </header>

      {banner && (
        <div className="border-b bg-sky-50 px-4 py-2 text-sm text-sky-800">{banner}</div>
      )}

      <main className="min-h-0 flex-1 overflow-auto bg-gray-50 p-4">
        {!scopeReady ? (
          <Empty>Select a workload or subscription to load its backup estate.</Empty>
        ) : capsQ.isError ? (
          <Empty>{(capsQ.error as Error)?.message ?? "Could not resolve an Azure connection for this scope."}</Empty>
        ) : (
          <>
            {caps?.demo && (
              <div className="mb-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-800">
                Demo workload — synthetic backup estate. Every write action is disabled.
              </div>
            )}
            {caps?.analysis_only_scope && (
              <div className="mb-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-800">
                Management-group scope is analysis-only. Narrow to a workload or subscription to draft or apply changes.
              </div>
            )}
            {snapshot?.partial && (
              <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                Partial analysis — review source warnings and workbook Coverage &amp; limitations before interpreting totals.
              </div>
            )}
            {progressState && (
              <div className="mb-3"><AnalysisProgress state={progressState} /></div>
            )}
            {snapshotQ.isLoading ? (
              <Empty>Loading the last backup analysis…</Empty>
            ) : snapshotQ.isError ? (
              <Empty>{(snapshotQ.error as Error).message}</Empty>
            ) : !analyzed ? (
              // Every tab shows the same prompt: nothing is fetched until it is asked for.
              <NeedsAnalysis onAnalyze={startAnalysis} analyzing={analyzing}
                startedAt={analyzingSince} />
            ) : !snapshot ? null : (
              <>
                {tab === "overview" && <OverviewTab snapshot={snapshot} actionable={actionable} onGoTab={goTab} />}
                {tab === "flow" && (
                  <BackupFlowTab snapshot={snapshot} scope={scope} onGoTab={(next) => goTab(next as Tab)}
                    onFocusGap={(ids) => setFocusGapIds(ids)} onFocusVault={(id) => setFocusVaultId(id)} />
                )}
                {tab === "inventory" && <InventoryTab snapshot={snapshot} scope={scope} caps={caps} onBanner={setBanner} portalHost={portalHost} />}
                {tab === "jobs" && <JobsTab snapshot={snapshot} scope={scope} caps={caps} onBanner={setBanner} portalHost={portalHost} />}
                {tab === "policies" && <PoliciesTab snapshot={snapshot} scope={scope} caps={caps} portalHost={portalHost} />}
                {tab === "vaults" && <VaultsTab snapshot={snapshot} scope={scope} caps={caps} onBanner={setBanner} focusVaultId={focusVaultId} portalHost={portalHost} />}
                {tab === "gaps" && <GapsTab snapshot={snapshot} scope={scope} caps={caps} onBanner={setBanner} onGoTab={goTab} focusGap={routeSearch.get("gap") ?? ""} focusGapIds={focusGapIds} portalHost={portalHost} />}
                {tab === "dr" && <DrTab snapshot={snapshot} scope={scope} caps={caps} onBanner={setBanner} portalHost={portalHost} />}
                {tab === "cost" && <CostTab snapshot={snapshot} scope={scope} portalHost={portalHost} />}
                {tab === "changes" && <ChangesTab scope={scope} caps={caps} onBanner={setBanner} portalHost={portalHost} />}
              </>
            )}
          </>
        )}
      </main>
      </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- Overview
function OverviewTab({ snapshot, actionable, onGoTab }: {
  snapshot: BackupSnapshot; actionable: number; onGoTab: (t: Tab) => void;
}) {
  // The ledger moves without an analysis, so the live count wins over the snapshot's.
  const s = { ...snapshot.summary, actionable_changes: actionable || snapshot.summary.actionable_changes };
  const jobBand = s.jobs.failed > 0 ? "red" : s.jobs.total === 0 ? undefined : "green";

  return (
    <div className="space-y-4">
      <ErrorBanner errors={s.errors} />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Protected items" value={s.protection.protected_items}
          hint={`${s.protection.vaults} vault(s) · ${s.protection.policies} policies`} />
        <Stat label={`Jobs (${s.jobs.window_hours}h)`} value={`${s.jobs.succeeded}/${s.jobs.total || 0}`}
          hint={s.jobs.failed ? `${s.jobs.failed} failed` : "No failures"} band={jobBand} />
        <Stat label="RPO attainment" value={`${s.rpo.attainment_pct}%`}
          hint={`${s.rpo.breached} breached · ${s.rpo.at_risk} at risk`}
          band={s.rpo.breached ? "red" : s.rpo.at_risk ? "amber" : "green"} />
        <Stat label="Ransomware readiness" value={s.posture.average_score}
          hint={`${s.posture.red_vaults} vault(s) at risk`} band={s.posture.band} />
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Chronic failures" value={s.chronic_failures}
          hint="No recent recovery point" band={s.chronic_failures ? "red" : "green"} />
        <Stat label="Site Recovery health" value={`${s.dr.health_pct}%`}
          hint={`${s.dr.replicated_items} replicated · ${s.dr.stale_drills} stale drill(s)`}
          band={s.dr.unhealthy ? "red" : s.dr.stale_drills ? "amber" : "green"} />
        <Stat label="Estimated monthly cost" value={fmtMoney(s.cost.monthly_total, s.cost.currency)}
          hint={`${s.cost.confidence} · ${fmtMoney(s.cost.recoverable_monthly, s.cost.currency)} recoverable`} />
        <Stat label="Changes awaiting you" value={s.actionable_changes}
          hint="Pending decisions + approved, not yet applied" band={s.actionable_changes ? "amber" : undefined} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">What to do next</h2>
          <ul className="mt-2 space-y-2 text-sm">
            {s.jobs.failed > 0 && (
              <li><button className="text-brand-dark underline" onClick={() => onGoTab("jobs")}>
                Triage {s.jobs.failed} failed backup job(s)</button></li>
            )}
            {s.chronic_failures > 0 && (
              <li><button className="text-brand-dark underline" onClick={() => onGoTab("jobs")}>
                {s.chronic_failures} item(s) have no recent recovery point</button></li>
            )}
            {s.posture.actionable_count > 0 && (
              <li><button className="text-brand-dark underline" onClick={() => onGoTab("vaults")}>
                {s.posture.actionable_count} vault hardening action(s) available</button></li>
            )}
            {s.protection.orphaned > 0 && (
              <li><button className="text-brand-dark underline" onClick={() => onGoTab("cost")}>
                {s.protection.orphaned} orphaned protected item(s) still billing</button></li>
            )}
            {s.actionable_changes > 0 && (
              <li><button className="text-brand-dark underline" onClick={() => onGoTab("changes")}>
                {s.actionable_changes} managed change(s) awaiting approval or apply</button></li>
            )}
            {!s.jobs.failed && !s.chronic_failures && !s.posture.actionable_count && !s.actionable_changes && (
              <li className="text-gray-500">Nothing urgent. Check the Gaps tab for unprotected resources.</li>
            )}
          </ul>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Scope of this module</h2>
          <p className="mt-2 text-sm text-gray-600">
            Backup Manager reads the whole estate from Azure Resource Graph and drafts approval-gated changes.
            It does <strong>not</strong> restore data and does <strong>not</strong> delete backup data, purge
            soft-deleted items, or lock vault immutability — those stay in the Azure portal on purpose, because
            they cannot be rolled back.
          </p>
          <p className="mt-2 text-xs text-gray-500">
            Backup job history from Resource Graph covers the last {s.job_window_days} days. Enable vault
            diagnostics from the Vaults tab for long-horizon reporting.
          </p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- Inventory
function InventoryTab({ snapshot, scope, caps, onBanner, portalHost }: {
  snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; portalHost: string;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [vaultId, setVaultId] = useState("");
  const [onlyIssues, setOnlyIssues] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<BackupInstance | null>(null);
  const PAGE_SIZE = 100;

  // Filtering happens over the analyzed rows: no request, so typing in the box cannot
  // trigger a fresh Azure read.
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return snapshot.inventory.rows.filter((row) => {
      if (vaultId && (row.vault_id || "").toLowerCase() !== vaultId.toLowerCase()) return false;
      if (onlyIssues && !(row.orphaned || row.protection_stopped || row.last_error_code
        || ["failed", "unhealthy"].includes((row.last_backup_status || "").toLowerCase()))) return false;
      if (!needle) return true;
      return `${row.friendly_name} ${row.datasource_id} ${row.policy_name}`.toLowerCase().includes(needle);
    });
  }, [snapshot.inventory.rows, search, vaultId, onlyIssues]);
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const backupNowM = useMutation({
    mutationFn: (instanceId: string) => api.backupManagerBackupNow({ ...scope, instance_id: instanceId }),
    onSuccess: (r) => {
      onBanner(`On-demand backup drafted as change ${r.change.id.slice(0, 8)} — approve it in Managed changes.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const stopM = useMutation({
    mutationFn: (instanceId: string) =>
      api.backupManagerProtectionChange({ ...scope, action: "stop_retain_data", target_id: instanceId }),
    onSuccess: (r) => {
      onBanner(`Stop-protection (data retained) drafted as change ${r.change.id.slice(0, 8)}.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });

  const data = {
    rows: pageRows,
    total_count: filtered.length,
    facets: snapshot.inventory.facets,
    errors: snapshot.errors,
    has_more: page * PAGE_SIZE < filtered.length,
  };
  const canWrite = caps?.can_protect && !caps?.read_only && !caps?.demo && !caps?.analysis_only_scope;

  return (
    <div className="space-y-3">
      <ErrorBanner errors={data.errors} />
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          placeholder="Search item, datasource or policy…"
          className="w-64 rounded-lg border px-3 py-1.5 text-sm"
        />
        <select value={vaultId} onChange={(e) => { setVaultId(e.target.value); setPage(1); }}
          className="rounded-lg border px-2 py-1.5 text-sm">
          <option value="">All vaults</option>
          {data.facets.vaults.map((v) => (
            <option key={v.id} value={v.id}>{v.name} ({v.count})</option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-sm text-gray-600">
          <input type="checkbox" checked={onlyIssues} onChange={(e) => { setOnlyIssues(e.target.checked); setPage(1); }} />
          Only items with issues
        </label>
        <span className="ml-auto text-xs text-gray-500">{data.total_count} item(s)</span>
        <button
          onClick={async () => downloadBlob(await api.backupManagerExport("instances", scope), "backup-instances.csv")}
          className="rounded-lg border px-2.5 py-1.5 text-xs hover:bg-gray-50"
        >Export CSV</button>
      </div>

      {data.rows.length === 0 ? (
        <Empty>No protected items match this filter.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Item</th>
                <th className="px-3 py-2">Type</th>
                <th className="px-3 py-2">Vault</th>
                <th className="px-3 py-2">Policy</th>
                <th className="px-3 py-2">State</th>
                <th className="px-3 py-2">Latest RP</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.rows.map((row) => (
                <tr key={row.id} className="hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5">
                      <button className="font-medium text-gray-900 hover:underline" onClick={() => setSelected(row)}>
                        {row.friendly_name || row.name}
                      </button>
                      {!row.orphaned && <PortalLink resourceId={row.datasource_id} portalHost={portalHost} label="Open source resource" />}
                      <PortalLink resourceId={row.id} portalHost={portalHost} label="Open protected item" />
                    </div>
                    <div className="flex gap-1 pt-0.5">
                      {row.orphaned && <Pill tone="bg-rose-50 text-rose-700">orphaned</Pill>}
                      {row.protection_stopped && <Pill tone="bg-amber-50 text-amber-700">stopped</Pill>}
                      {row.last_error_code && <Pill tone="bg-rose-50 text-rose-700">{row.last_error_code}</Pill>}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-600">{row.datasource_type}</td>
                  <td className="px-3 py-2 text-gray-600">
                    <span className="inline-flex items-center gap-1">{row.vault_name}<PortalLink resourceId={row.vault_id} portalHost={portalHost} label="Open vault" /></span>
                    <div className="text-[11px] text-gray-400">
                      {row.vault_kind === "backup" ? "Backup vault" : "Recovery Services"}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-600">
                    <span className="inline-flex items-center gap-1">{row.policy_name || "—"}<PortalLink resourceId={row.policy_id} portalHost={portalHost} label="Open policy" /></span>
                  </td>
                  <td className="px-3 py-2">{row.protection_state || "—"}</td>
                  <td className="px-3 py-2">
                    {fmtAge(row.recovery_point_age_hours)}
                    {row.recovery_point_source === "job" && (
                      <span className="ml-1 text-[10px] text-gray-400" title="Derived from the latest successful backup job">≈</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {canWrite && (
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => backupNowM.mutate(row.id)}
                          disabled={backupNowM.isPending || row.protection_stopped}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-40"
                        >Back up now</button>
                        {!row.protection_stopped && (
                          <button
                            onClick={() => stopM.mutate(row.id)}
                            disabled={stopM.isPending}
                            className="rounded border px-2 py-1 text-xs text-amber-700 hover:bg-amber-50 disabled:opacity-40"
                            title="Stops future backups but keeps every existing recovery point"
                          >Stop (retain data)</button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.total_count > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm">
          <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}
            className="rounded-lg border px-3 py-1.5 disabled:opacity-40">← Previous</button>
          <span className="text-xs text-gray-500">
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, data.total_count)} of {data.total_count}
          </span>
          <button disabled={!data.has_more} onClick={() => setPage((p) => p + 1)}
            className="rounded-lg border px-3 py-1.5 disabled:opacity-40">Next →</button>
        </div>
      )}

      {selected && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={() => setSelected(null)}>
          <div className="h-full w-full max-w-lg overflow-auto bg-white p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <h3 className="text-base font-semibold">{selected.friendly_name}</h3>
              <button onClick={() => setSelected(null)} className="text-gray-400 hover:text-gray-600">✕</button>
            </div>
            <dl className="mt-3 space-y-2 text-sm">
              {[
                ["Datasource", selected.datasource_id],
                ["Type", selected.datasource_type],
                ["Vault", `${selected.vault_name} (${selected.vault_kind === "backup" ? "Backup vault" : "Recovery Services vault"})`],
                ["Policy", selected.policy_name],
                ["Protection state", selected.protection_state],
                ["Protection status", selected.protection_status],
                ["Last backup", `${selected.last_backup_status || "—"} · ${fmtDate(selected.last_backup_time)}`],
                ["Latest recovery point", fmtDate(selected.latest_recovery_point)],
                ["Redundancy", selected.vault_redundancy || "—"],
                ["Subscription", selected.subscription_id],
              ].map(([label, value]) => (
                <div key={label as string}>
                  <dt className="text-[11px] uppercase tracking-wide text-gray-500">{label}</dt>
                  <dd className="break-all text-gray-800">{value || "—"}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-3 flex flex-wrap gap-2 border-t pt-3 text-xs">
              {selected.orphaned ? (
                <span className="rounded bg-gray-100 px-2 py-1 text-gray-600">Source deleted</span>
              ) : (
                <PortalLink resourceId={selected.datasource_id} portalHost={portalHost} label="Open source resource" className="rounded border px-2 py-1" />
              )}
              <PortalLink resourceId={selected.id} portalHost={portalHost} label="Open protected item" className="rounded border px-2 py-1" />
              <PortalLink resourceId={selected.vault_id} portalHost={portalHost} label="Open vault" className="rounded border px-2 py-1" />
              <PortalLink resourceId={selected.policy_id} portalHost={portalHost} label="Open policy" className="rounded border px-2 py-1" />
              <PortalLink subscriptionId={selected.subscription_id} portalHost={portalHost} label="Open subscription" className="rounded border px-2 py-1" />
            </div>
            {selected.last_error_message && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">
                <div className="font-medium">{selected.last_error_code}</div>
                <div className="mt-1">{selected.last_error_message}</div>
              </div>
            )}
            {selected.orphaned && (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                The source resource no longer exists. Backup data is still retained and billed. Removing backup
                data is a portal-only operation — Backup Manager will not delete recovery points.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- Jobs
function JobsTab({ snapshot, scope, caps, onBanner, portalHost }: {
  snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; portalHost: string;
}) {
  const qc = useQueryClient();
  const [status, setStatus] = useState("failed");
  const [search, setSearch] = useState("");
  const [openCluster, setOpenCluster] = useState<string>("");
  type ChronicSortKey = "item" | "vault" | "age" | "error";
  type JobSortKey = "started" | "item" | "operation" | "status" | "cause";
  const chronicSorter = useTableSort<ChronicSortKey>(
    "azsup.backupManager.chronicSort", { key: "age", direction: "desc" },
    ["item", "vault", "age", "error"],
  );
  const jobSorter = useTableSort<JobSortKey>(
    "azsup.backupManager.jobSort", { key: "started", direction: "desc" },
    ["started", "item", "operation", "status", "cause"],
  );

  const chronicRows = useMemo(() => {
    const rows = [...(snapshot.job_analysis?.chronic ?? [])];
    const { key, direction } = chronicSorter.sort;
    rows.sort((a, b) => {
      let result = 0;
      if (key === "age") {
        if (a.age_days === null && b.age_days !== null) return direction === "desc" ? -1 : 1;
        if (a.age_days !== null && b.age_days === null) return direction === "desc" ? 1 : -1;
        result = compareNumberDirection(a.age_days, b.age_days, direction);
      } else if (key === "item") result = compareTextDirection(a.name, b.name, direction);
      else if (key === "vault") result = compareTextDirection(a.vault_name, b.vault_name, direction);
      else result = compareTextDirection(a.error_code, b.error_code, direction);
      if (result) return result;
      return compareText(a.name, b.name) || compareText(a.instance_id, b.instance_id);
    });
    return rows;
  }, [snapshot.job_analysis?.chronic, chronicSorter.sort]);

  const filteredJobRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const rows = snapshot.jobs.rows.filter((row) => {
      if (status && row.status_bucket !== status) return false;
      if (!needle) return true;
      return `${row.entity_name} ${row.error_code} ${row.operation}`.toLowerCase().includes(needle);
    });
    const statusRank: Record<string, number> = { failed: 0, running: 1, unknown: 2, succeeded: 3 };
    const { key, direction } = jobSorter.sort;
    rows.sort((a, b) => {
      let result = 0;
      if (key === "started") result = compareNumberDirection(Date.parse(a.start_time), Date.parse(b.start_time), direction);
      else if (key === "item") result = compareTextDirection(a.entity_name, b.entity_name, direction);
      else if (key === "operation") result = compareTextDirection(a.operation, b.operation, direction);
      else if (key === "status") {
        result = (statusRank[a.status_bucket] ?? 9) - (statusRank[b.status_bucket] ?? 9);
        if (direction === "desc") result = -result;
      } else result = compareTextDirection(a.failure_title || a.error_code, b.failure_title || b.error_code, direction);
      if (result) return result;
      return compareText(a.entity_name, b.entity_name) || compareText(a.id, b.id);
    });
    return rows;
  }, [snapshot.jobs.rows, status, search, jobSorter.sort]);
  const jobRows = filteredJobRows.slice(0, 100);

  const retryM = useMutation({
    mutationFn: (instanceId: string) => api.backupManagerBackupNow({ ...scope, instance_id: instanceId }),
    onSuccess: (r) => {
      onBanner(`Retry drafted as change ${r.change.id.slice(0, 8)} — approve it in Managed changes.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const cancelM = useMutation({
    mutationFn: (jobId: string) => api.backupManagerCancelJob({ ...scope, job_id: jobId }),
    onSuccess: (r) => {
      onBanner(`Job cancellation drafted as change ${r.change.id.slice(0, 8)}.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });

  const canWrite = caps?.can_ondemand && !caps?.read_only && !caps?.demo && !caps?.analysis_only_scope;
  const analysis = snapshot.job_analysis;

  return (
    <div className="space-y-4">
      {analysis && (
        <div className="grid gap-3 sm:grid-cols-4">
          <Stat label="Succeeded (24h)" value={analysis.summary.succeeded} />
          <Stat label="Failed (24h)" value={analysis.summary.failed} band={analysis.summary.failed ? "red" : "green"} />
          <Stat label="Running" value={analysis.summary.running} />
          <Stat label="Chronic failures" value={analysis.chronic.length}
            hint="Items with no recent recovery point" band={analysis.chronic.length ? "red" : "green"} />
        </div>
      )}

      {analysis && analysis.chronic.length > 0 && (
        <div className="rounded-xl border border-rose-200 bg-white">
          <div className="border-b bg-rose-50 px-4 py-2 text-sm font-semibold text-rose-800">
            Silently unprotected — no recent recovery point
          </div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <SortableTh label="Item" column="item" state={chronicSorter.sort} onSort={chronicSorter.update} className="px-3 py-2" />
                <SortableTh label="Vault" column="vault" state={chronicSorter.sort} onSort={chronicSorter.update} className="px-3 py-2" />
                <SortableTh label="Last recovery point" column="age" state={chronicSorter.sort} onSort={chronicSorter.update}
                  first="desc" directionLabels={{ asc: "Newest first", desc: "Oldest first" }} className="px-3 py-2" />
                <SortableTh label="Error" column="error" state={chronicSorter.sort} onSort={chronicSorter.update} className="px-3 py-2" />
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {chronicRows.slice(0, 50).map((row: BackupChronicFailure) => (
                <tr key={row.instance_id}>
                  <td className="px-3 py-2 font-medium">
                    <span className="inline-flex items-center gap-1">{row.name}<PortalLink resourceId={row.datasource_id} portalHost={portalHost} label="Open source resource" /><PortalLink resourceId={row.instance_id} portalHost={portalHost} label="Open protected item" /></span>
                  </td>
                  <td className="px-3 py-2 text-gray-600"><span className="inline-flex items-center gap-1">{row.vault_name}<PortalLink resourceId={row.vault_id} portalHost={portalHost} label="Open vault" /></span></td>
                  <td className="px-3 py-2">
                    {row.age_days === null ? <Pill tone="bg-rose-100 text-rose-800">never</Pill> : `${row.age_days}d ago`}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">{row.error_code || "—"}</td>
                  <td className="px-3 py-2 text-right">
                    {canWrite && (
                      <button onClick={() => retryM.mutate(row.instance_id)} disabled={retryM.isPending}
                        className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-40">
                        Back up now
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {chronicRows.length > 50 && <div className="border-t px-3 py-2 text-xs text-gray-500">Showing 50 of {chronicRows.length}</div>}
        </div>
      )}

      {analysis && analysis.clusters.length > 0 && (
        <div className="rounded-xl border bg-white">
          <div className="border-b px-4 py-2 text-sm font-semibold text-gray-900">
            Failure clusters — one root cause per row
          </div>
          <ul className="divide-y">
            {analysis.clusters.map((cluster: BackupFailureCluster) => (
              <li key={cluster.error_code} className="px-4 py-3">
                <button className="flex w-full items-start justify-between gap-3 text-left"
                  onClick={() => setOpenCluster(openCluster === cluster.error_code ? "" : cluster.error_code)}>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900">{cluster.title}</span>
                      {!cluster.known && <Pill tone="bg-gray-100 text-gray-600">unclassified</Pill>}
                      {cluster.retryable && <Pill tone="bg-emerald-50 text-emerald-700">retryable</Pill>}
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-gray-500">{cluster.error_code}</div>
                  </div>
                  <div className="whitespace-nowrap text-right text-xs text-gray-500">
                    <div className="text-base font-semibold text-gray-900">{cluster.job_count}</div>
                    {cluster.entity_count} item(s) · {cluster.subscription_count} sub(s)
                  </div>
                </button>
                {openCluster === cluster.error_code && (
                  <div className="mt-3 space-y-2 rounded-lg bg-gray-50 p-3 text-sm">
                    {cluster.cause && <p><span className="font-medium">Cause: </span>{cluster.cause}</p>}
                    {cluster.remediation && <p><span className="font-medium">Fix: </span>{cluster.remediation}</p>}
                    {!cluster.known && (
                      <p className="text-xs text-gray-500">
                        This error code is not in the knowledge base yet. An administrator can add it under
                        Settings → Backup failure knowledge base so the next occurrence is actionable.
                      </p>
                    )}
                    <div className="text-xs text-gray-600">
                      Affected: {cluster.entities.slice(0, 12).join(", ")}
                      {cluster.entity_count > 12 && ` and ${cluster.entity_count - 12} more`}
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="rounded-lg border px-2 py-1.5 text-sm">
          <option value="">All jobs</option>
          <option value="failed">Failed</option>
          <option value="running">Running</option>
          <option value="succeeded">Succeeded</option>
        </select>
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search item or error code…"
          className="w-64 rounded-lg border px-3 py-1.5 text-sm" />
        <span className="ml-auto text-xs text-gray-500">
          Resource Graph retains ~{snapshot.jobs.job_window_days ?? 7} days of job history
        </span>
        {filteredJobRows.length > 100 && <span className="text-xs text-gray-500">Showing 100 of {filteredJobRows.length}</span>}
        <button onClick={async () => downloadBlob(await api.backupManagerExport("jobs", scope), "backup-jobs.csv")}
          className="rounded-lg border px-2.5 py-1.5 text-xs hover:bg-gray-50">Export CSV</button>
      </div>

      {jobRows.length === 0 ? (
        <Empty>No jobs match this filter.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <SortableTh label="Started" column="started" state={jobSorter.sort} onSort={jobSorter.update} first="desc" className="px-3 py-2" />
                <SortableTh label="Item" column="item" state={jobSorter.sort} onSort={jobSorter.update} className="px-3 py-2" />
                <SortableTh label="Operation" column="operation" state={jobSorter.sort} onSort={jobSorter.update} className="px-3 py-2" />
                <SortableTh label="Status" column="status" state={jobSorter.sort} onSort={jobSorter.update} className="px-3 py-2" />
                <SortableTh label="Cause" column="cause" state={jobSorter.sort} onSort={jobSorter.update} className="px-3 py-2" />
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {jobRows.map((job: BackupJob) => (
                <tr key={job.id} className="hover:bg-gray-50">
                  <td className="whitespace-nowrap px-3 py-2 text-gray-600">{fmtDate(job.start_time)}</td>
                  <td className="px-3 py-2 font-medium"><span className="inline-flex items-center gap-1">{job.entity_name || "—"}<PortalLink resourceId={job.datasource_id} portalHost={portalHost} label="Open source resource" /><PortalLink resourceId={job.id} portalHost={portalHost} label="Open backup job" /><PortalLink resourceId={job.vault_id} portalHost={portalHost} label="Open vault" /></span></td>
                  <td className="px-3 py-2 text-gray-600">{job.operation}</td>
                  <td className="px-3 py-2">
                    <Pill tone={STATUS_STYLE[job.status_bucket] ?? STATUS_STYLE.unknown}>{job.status}</Pill>
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">
                    {job.failure_title || job.error_code || "—"}
                    {job.failure_remediation && (
                      <div className="mt-0.5 text-[11px] text-gray-500">{job.failure_remediation}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {canWrite && job.status_bucket === "running" && (
                      <button onClick={() => cancelM.mutate(job.id)} disabled={cancelM.isPending}
                        className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-40">Cancel</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- Policies
function PoliciesTab({ snapshot, scope, caps, portalHost }: { snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; portalHost: string }) {
  const [impactFor, setImpactFor] = useState<BackupPolicy | null>(null);
  const [proposed, setProposed] = useState(30);
  type PolicySortKey = "attention" | "policy" | "vault" | "schedule" | "retention" | "items";
  const policySorter = useTableSort<PolicySortKey>(
    "azsup.backupManager.policySort", { key: "vault", direction: "asc" },
    ["attention", "policy", "vault", "schedule", "retention", "items"],
  );
  const impactM = useMutation({
    mutationFn: () => api.backupManagerRetentionImpact({
      ...scope, policy_id: impactFor!.id, proposed_retention_days: proposed, exact: true,
    }),
  });

  const data = snapshot.policies;
  const sortedPolicies = useMemo(() => {
    const rows = [...data.policies];
    const { key, direction } = policySorter.sort;
    const attentionRank = (policy: BackupPolicy) => policy.below_floor
      ? 0 : policy.unused && policy.duplicate_of.length > 0
        ? 1 : policy.duplicate_of.length > 0
          ? 2 : policy.unused ? 3 : 4;
    rows.sort((a, b) => {
      let result = 0;
      if (key === "attention") result = attentionRank(a) - attentionRank(b);
      else if (key === "policy") result = compareTextDirection(a.name, b.name, direction);
      else if (key === "vault") result = compareTextDirection(a.vault_name, b.vault_name, direction);
      else if (key === "schedule") result = compareTextDirection(a.schedule_summary, b.schedule_summary, direction);
      else if (key === "retention") result = compareNumberDirection(a.retention_days, b.retention_days, direction);
      else result = compareNumberDirection(a.in_use_count, b.in_use_count, direction);
      if (key === "attention" && direction === "desc") result = -result;
      if (result) return result;
      return compareText(a.vault_name, b.vault_name) || compareText(a.name, b.name) || compareText(a.id, b.id);
    });
    return rows;
  }, [data.policies, policySorter.sort]);

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="Policies" value={data.summary.total} />
        <Stat label="Unused" value={data.summary.unused} hint="No protected items" band={data.summary.unused ? "amber" : "green"} />
        <Stat label="Below baseline" value={data.summary.below_floor}
          hint={`< ${data.summary.retention_floor_days} days retention`} band={data.summary.below_floor ? "red" : "green"} />
        <Stat label="Duplicate groups" value={data.summary.duplicate_groups}
          hint={`${data.summary.duplicate_policies} near-identical policies`} band={data.summary.duplicate_groups ? "amber" : "green"} />
      </div>

      {data.duplicate_groups.length > 0 && (
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Policy sprawl</h2>
          <p className="mt-1 text-xs text-gray-500">
            Structurally identical policies duplicated across vaults — every retention decision has to be made once per copy.
          </p>
          <ul className="mt-2 space-y-2 text-sm">
            {data.duplicate_groups.map((group) => (
              <li key={group.fingerprint} className="rounded-lg bg-gray-50 p-3">
                <div className="font-medium">{group.names.join(", ")}</div>
                <div className="text-xs text-gray-600">
                  {group.policy_count} copies across {group.vault_count} vault(s) · {group.protected_items} protected item(s)
                  {group.retention_days ? ` · ${group.retention_days}d retention` : ""}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        <label className="text-xs text-gray-500" htmlFor="backup-policy-sort">Sort policies</label>
        <select
          id="backup-policy-sort"
          value={policySorter.sort.key}
          onChange={(event) => policySorter.replace({ key: event.target.value as PolicySortKey, direction: "asc" })}
          className="rounded-lg border px-2 py-1.5 text-xs"
        >
          <option value="attention">Attention first</option>
          <option value="vault">Vault</option>
          <option value="policy">Policy</option>
          <option value="schedule">Schedule</option>
          <option value="retention">Retention</option>
          <option value="items">Protected items</option>
        </select>
        <button
          type="button"
          onClick={() => policySorter.update(policySorter.sort.key)}
          className="rounded-lg border px-2 py-1 text-xs hover:bg-gray-50"
          aria-label="Reverse policy sort direction"
        >
          {policySorter.sort.direction === "asc" ? "▲" : "▼"}
        </button>
      </div>

      <div className="overflow-x-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <SortableTh label="Policy" column="policy" state={policySorter.sort} onSort={policySorter.update} className="px-3 py-2" />
              <SortableTh label="Vault" column="vault" state={policySorter.sort} onSort={policySorter.update} className="px-3 py-2" />
              <SortableTh label="Schedule" column="schedule" state={policySorter.sort} onSort={policySorter.update} className="px-3 py-2" />
              <SortableTh label="Retention" column="retention" state={policySorter.sort} onSort={policySorter.update} className="px-3 py-2" />
              <SortableTh label="Items" column="items" state={policySorter.sort} onSort={policySorter.update} className="px-3 py-2" />
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y">
            {sortedPolicies.map((policy) => (
              <tr key={policy.id} className="hover:bg-gray-50">
                <td className="px-3 py-2">
                  <div className="flex items-center gap-1.5 font-medium">{policy.name}<PortalLink resourceId={policy.arm_id || policy.id} portalHost={portalHost} label="Open policy" /></div>
                  <div className="flex gap-1 pt-0.5">
                    {policy.below_floor && <Pill tone="bg-rose-50 text-rose-700">below baseline</Pill>}
                    {policy.unused && <Pill tone="bg-gray-100 text-gray-600">unused</Pill>}
                    {policy.duplicate_of.length > 0 && <Pill tone="bg-amber-50 text-amber-700">duplicated</Pill>}
                  </div>
                </td>
                <td className="px-3 py-2 text-gray-600"><span className="inline-flex items-center gap-1">{policy.vault_name}<PortalLink resourceId={policy.vault_id} portalHost={portalHost} label="Open vault" /></span></td>
                <td className="px-3 py-2 text-gray-600">{policy.schedule_summary || "—"}</td>
                <td className="px-3 py-2">{policy.retention_days ? `${policy.retention_days}d` : "—"}</td>
                <td className="px-3 py-2">{policy.in_use_count}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    disabled={caps?.analysis_only_scope}
                    title={caps?.analysis_only_scope ? "Narrow to a workload or subscription to model a retention change." : undefined}
                    onClick={() => { setImpactFor(policy); setProposed(policy.retention_days ?? 30); impactM.reset(); }}
                    className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
                  >Model retention change</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {impactFor && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30 p-4" onClick={() => setImpactFor(null)}>
          <div className="w-full max-w-2xl rounded-xl bg-white p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-base font-semibold">Retention impact — {impactFor.name}</h3>
                <p className="text-xs text-gray-500">
                  Current retention {impactFor.retention_days ?? "unknown"} days across {impactFor.in_use_count} item(s).
                </p>
              </div>
              <button onClick={() => setImpactFor(null)} className="text-gray-400 hover:text-gray-600">✕</button>
            </div>
            <div className="mt-3 flex items-end gap-2">
              <label className="text-sm">
                <span className="block text-xs text-gray-500">Proposed retention (days)</span>
                <input type="number" min={1} value={proposed}
                  onChange={(e) => setProposed(Number(e.target.value))}
                  className="mt-1 w-32 rounded-lg border px-2 py-1.5" />
              </label>
              <button onClick={() => impactM.mutate()} disabled={impactM.isPending}
                className="rounded-lg bg-brand-dark px-3 py-1.5 text-sm text-white disabled:opacity-50">
                {impactM.isPending ? "Modeling…" : "Model impact"}
              </button>
            </div>
            {impactM.isError && <p className="mt-2 text-sm text-rose-700">{(impactM.error as Error).message}</p>}
            {impactM.data && (
              <div className="mt-3 space-y-2">
                <div className={`rounded-lg p-3 text-sm ${impactM.data.irreversible ? "bg-rose-50 text-rose-800" : "bg-emerald-50 text-emerald-800"}`}>
                  <div className="font-medium">
                    {impactM.data.direction === "decrease"
                      ? `${impactM.data.recovery_points_removed} recovery point(s) would be permanently removed`
                      : impactM.data.direction === "increase"
                        ? "No recovery points are removed by an increase"
                        : "No change"}
                  </div>
                  <div className="mt-1 text-xs">{impactM.data.note}</div>
                  <div className="mt-1 text-xs">
                    {impactM.data.exact_items} item(s) enumerated exactly, {impactM.data.estimated_items} estimated from
                    the schedule ({impactM.data.points_per_day}/day).
                  </div>
                </div>
                {impactM.data.per_instance.length > 0 && (
                  <div className="max-h-56 overflow-auto rounded-lg border">
                    <table className="w-full text-xs">
                      <thead className="bg-gray-50 text-left text-gray-500">
                        <tr><th className="px-2 py-1">Item</th><th className="px-2 py-1">Points removed</th><th className="px-2 py-1">Source</th></tr>
                      </thead>
                      <tbody className="divide-y">
                        {impactM.data.per_instance.map((row) => (
                          <tr key={row.instance_id}>
                            <td className="px-2 py-1">{row.name}</td>
                            <td className="px-2 py-1">{row.recovery_points_removed}</td>
                            <td className="px-2 py-1 text-gray-500">{row.estimated ? "estimated" : "exact"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- Vaults
const HARDEN_LABEL: Record<string, string> = {
  enable_soft_delete: "Enable soft delete",
  extend_soft_delete_retention: "Extend soft-delete retention",
  enable_crr: "Enable Cross Region Restore",
  set_redundancy: "Set geo-redundant storage",
  enable_vault_alerts: "Enable built-in alerts",
  enable_diagnostics: "Send Backup Reports to Log Analytics",
};

function VaultsTab({ snapshot, scope, caps, onBanner, focusVaultId, portalHost }: {
  snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; focusVaultId?: string; portalHost: string;
}) {
  const qc = useQueryClient();
  const [openVault, setOpenVault] = useState<string>("");
  const [selectedControls, setSelectedControls] = useState<Record<string, string[]>>({});
  const [workspaceId, setWorkspaceId] = useState("");

  // Arriving from the flow chart should land on the vault that was clicked.
  useEffect(() => { if (focusVaultId) setOpenVault(focusVaultId); }, [focusVaultId]);

  const hardenM = useMutation({
    mutationFn: (vars: { vaultId: string; controls: string[] }) =>
      api.backupManagerHarden({ ...scope, vault_id: vars.vaultId, controls: vars.controls, workspace_id: workspaceId }),
    onSuccess: (r) => {
      onBanner(`${r.created} hardening change(s) drafted. Approve them in Managed changes.`);
      setSelectedControls({});
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });

  const data = snapshot.posture;
  const canWrite = caps?.can_manage_vaults && !caps?.read_only && !caps?.demo && !caps?.analysis_only_scope;
  const vaultDetails = useMemo(
    () => new Map(snapshot.vaults.vaults.map((vault) => [vault.id.toLowerCase(), vault])),
    [snapshot.vaults.vaults],
  );

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="Average readiness" value={data.average_score} band={data.band} />
        <Stat label="Vaults at risk" value={data.red_vaults} band={data.red_vaults ? "red" : "green"} />
        <Stat label="Actions available" value={data.actionable_count} />
        <Stat label="Vaults" value={data.vault_count} />
      </div>

      <div className="rounded-xl border bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900">Controls across the fleet</h2>
        <div className="mt-2 space-y-1">
          {data.by_check.map((check) => {
            const total = check.pass + check.warn + check.fail;
            const pct = total ? Math.round((100 * check.pass) / total) : 100;
            return (
              <div key={check.id} className="flex items-center gap-3 text-sm">
                <div className="w-64 truncate text-gray-700" title={check.label}>
                  {check.label}
                  {check.portal_only && <span className="ml-1 text-[10px] text-gray-400" title={check.portal_reason}>portal-only</span>}
                </div>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-100">
                  <div className={`h-full ${pct >= 80 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-rose-500"}`}
                    style={{ width: `${pct}%` }} />
                </div>
                <div className="w-28 text-right text-xs text-gray-500">
                  {check.pass} pass · {check.fail} fail
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="space-y-2">
        {data.vaults.map((vault: BackupVaultScore) => {
          const isOpen = openVault === vault.vault_id;
          const chosen = selectedControls[vault.vault_id] ?? [];
          const detail = vaultDetails.get(vault.vault_id.toLowerCase());
          const available = Array.from(new Set(
            vault.checks.filter((c) => c.action && !c.portal_only).map((c) => c.action),
          ));
          return (
            <div key={vault.vault_id} className="rounded-xl border bg-white">
              <div className="flex items-center">
                <button className="flex min-w-0 flex-1 items-center justify-between gap-3 px-4 py-3 text-left"
                  onClick={() => setOpenVault(isOpen ? "" : vault.vault_id)}>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900">{vault.vault_name}</span>
                      <Pill tone={BAND_STYLE[vault.band]}>{vault.score}</Pill>
                      <span className="text-xs text-gray-500">
                        {vault.vault_kind === "backup" ? "Backup vault" : "Recovery Services vault"}
                      </span>
                    </div>
                    <div className="text-xs text-gray-500">
                      {vault.instance_count} protected item(s) · {vault.location} · {vault.subscription_id}
                    </div>
                  </div>
                  <span className="text-gray-400">{isOpen ? "▲" : "▼"}</span>
                </button>
                <PortalLink resourceId={vault.vault_id} portalHost={portalHost} label="Open vault" className="mr-4 rounded border px-2 py-1 text-xs" />
              </div>
              {isOpen && (
                <div className="border-t px-4 py-3">
                  <table className="w-full text-sm">
                    <tbody className="divide-y">
                      {vault.checks.map((check) => (
                        <tr key={check.id}>
                          <td className="py-1.5 pr-3">
                            <div className="font-medium text-gray-800">{check.label}</div>
                            {check.detail && <div className="text-xs text-gray-500">{check.detail}</div>}
                            {check.portal_only && check.status !== "pass" && (
                              <div className="text-xs text-indigo-700">{check.portal_reason}</div>
                            )}
                          </td>
                          <td className="w-40 py-1.5">
                            <Pill tone={STATUS_STYLE[check.status]}>{check.value}</Pill>
                          </td>
                          <td className="w-48 py-1.5 text-right">
                            {check.action && !check.portal_only && canWrite && (
                              <label className="inline-flex items-center gap-1.5 text-xs text-gray-700">
                                <input
                                  type="checkbox"
                                  checked={chosen.includes(check.action)}
                                  onChange={(e) => setSelectedControls((prev) => {
                                    const current = new Set(prev[vault.vault_id] ?? []);
                                    if (e.target.checked) current.add(check.action); else current.delete(check.action);
                                    return { ...prev, [vault.vault_id]: Array.from(current) };
                                  })}
                                />
                                {HARDEN_LABEL[check.action] ?? check.action}
                              </label>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {(detail?.diagnostics_workspaces.length ?? 0) > 0 && (
                    <div className="mt-3 flex flex-wrap items-center gap-2 border-t pt-3 text-xs text-gray-600">
                      <span className="font-medium text-gray-800">Diagnostic workspaces</span>
                      {detail!.diagnostics_workspaces.map((workspaceId, index) => (
                        <PortalLink
                          key={workspaceId}
                          resourceId={workspaceId}
                          portalHost={portalHost}
                          label={`Open diagnostic workspace ${index + 1}`}
                          className="rounded border px-2 py-1"
                        />
                      ))}
                    </div>
                  )}
                  {canWrite && available.length > 0 && (
                    <div className="mt-3 flex flex-wrap items-center gap-2 border-t pt-3">
                      {chosen.includes("enable_diagnostics") && (
                        <input value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}
                          placeholder="Log Analytics workspace ARM id"
                          className="w-96 rounded-lg border px-2 py-1.5 text-xs" />
                      )}
                      <button
                        onClick={() => hardenM.mutate({ vaultId: vault.vault_id, controls: chosen })}
                        disabled={!chosen.length || hardenM.isPending}
                        className="rounded-lg bg-brand-dark px-3 py-1.5 text-sm text-white disabled:opacity-40"
                      >
                        {hardenM.isPending ? "Drafting…" : `Draft ${chosen.length || ""} hardening change(s)`}
                      </button>
                      <span className="text-xs text-gray-500">Creates pending changes; nothing is applied yet.</span>
                    </div>
                  )}
                  {vault.portal_only_gaps.length > 0 && (
                    <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-xs text-indigo-900">
                      <div className="font-medium">Configure in the Azure portal</div>
                      <ul className="mt-1 list-disc pl-4">
                        {vault.portal_only_gaps.map((gap) => (
                          <li key={gap.id}><span className="font-medium">{gap.label}</span> — {gap.reason}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {data.capacity.some((c) => c.at_risk) && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <div className="font-medium">Vaults approaching a service limit</div>
          <ul className="mt-1 list-disc pl-4 text-xs">
            {data.capacity.filter((c) => c.at_risk).map((c) => (
              <li key={c.vault_id}>{c.vault_name}: {c.instances}/{c.instance_limit} items ({c.instance_pct}%)</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- Gaps
function GapsTab({
  snapshot, scope, caps, onBanner, onGoTab, focusGap, focusGapIds, portalHost,
}: { snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; onGoTab: (t: Tab) => void; focusGap: string; focusGapIds?: string[]; portalHost: string }) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [vaultId, setVaultId] = useState("");
  const [policyId, setPolicyId] = useState("");
  const [preview, setPreview] = useState<BackupRemediationItem[] | null>(null);
  type GapSortKey = "resource" | "type" | "resource_group" | "region" | "severity";
  const gapSorter = useTableSort<GapSortKey>(
    "azsup.backupManager.gapSort", { key: "severity", direction: "desc" },
    ["resource", "type", "resource_group", "region", "severity"],
  );

  useEffect(() => {
    if (focusGap) setSelected(new Set([focusGap]));
  }, [focusGap]);

  // Preselect everything the operator picked in the protection flow.
  useEffect(() => {
    if (focusGapIds?.length) setSelected(new Set(focusGapIds));
  }, [focusGapIds]);

  const previewM = useMutation({
    mutationFn: () => api.backupManagerRemediationPreview({
      ...scope, gap_ids: Array.from(selected), vault_id: vaultId, policy_id: policyId,
    }),
    onSuccess: (r) => setPreview(r.items),
    onError: (e: Error) => { setPreview(null); onBanner(e.message); },
  });
  const submitM = useMutation({
    mutationFn: () => api.backupManagerRemediationSubmit({
      ...scope, gap_ids: Array.from(selected), vault_id: vaultId, policy_id: policyId,
      reason: "Close backup protection gap",
    }),
    onSuccess: (r) => {
      onBanner(`${r.created} protection change(s) drafted. Approve them in Managed changes.`);
      setSelected(new Set());
      setPreview(null);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
      onGoTab("changes");
    },
    onError: (e: Error) => onBanner(e.message),
  });

  const data = snapshot.gaps;
  const canWrite = caps?.can_protect && !caps?.read_only && !caps?.demo && !caps?.analysis_only_scope;
  const policies = (data.policies ?? []).filter((p) => !vaultId || p.vault_id.toLowerCase() === vaultId.toLowerCase());
  const selectedVault = (data.vaults ?? []).find((vault) => vault.id === vaultId);
  const selectedPolicy = (data.policies ?? []).find((policy) => policy.id === policyId);
  const sortedGaps = useMemo(() => {
    const rows = [...data.gaps];
    const rank: Record<string, number> = { critical: 0, error: 1, warning: 2, info: 3 };
    const { key, direction } = gapSorter.sort;
    rows.sort((a, b) => {
      let result = 0;
      if (key === "severity") {
        // Ascending means lowest priority first; descending means highest priority first.
        result = (rank[a.severity.toLowerCase()] ?? 4) - (rank[b.severity.toLowerCase()] ?? 4);
        if (direction === "asc") result = -result;
      } else if (key === "resource") result = compareTextDirection(a.resource_name, b.resource_name, direction);
      else if (key === "type") result = compareTextDirection(a.display_type, b.display_type, direction);
      else if (key === "resource_group") result = compareTextDirection(a.resource_group, b.resource_group, direction);
      else result = compareTextDirection(a.location, b.location, direction);
      if (result) return result;
      return (rank[a.severity.toLowerCase()] ?? 4) - (rank[b.severity.toLowerCase()] ?? 4)
        || compareText(a.display_type, b.display_type)
        || compareText(a.resource_name, b.resource_name)
        || compareText(a.gap_id, b.gap_id);
    });
    return rows;
  }, [data.gaps, gapSorter.sort]);

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Backup coverage" value={`${data.coverage_pct}%`}
          hint={`${data.protected_total}/${data.eligible_total} eligible resources`}
          band={data.coverage_pct >= 90 ? "green" : data.coverage_pct >= 60 ? "amber" : "red"} />
        <Stat label="Unprotected" value={data.gaps.length} band={data.gaps.length ? "red" : "green"} />
        <Stat label="From Backup & DR Coverage" value={data.coverage_gaps.length}
          hint="Findings ingested from the read-only detector" />
      </div>
      {data.error && <ErrorBanner errors={{ detection: data.error }} />}

      {canWrite && (
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Remediate selected gaps</h2>
          <div className="mt-2 flex flex-wrap items-end gap-2">
            <label className="text-sm">
              <span className="block text-xs text-gray-500">Target vault</span>
              <select value={vaultId} onChange={(e) => { setVaultId(e.target.value); setPolicyId(""); setPreview(null); }}
                className="mt-1 w-64 rounded-lg border px-2 py-1.5">
                <option value="">Select a vault…</option>
                {(data.vaults ?? []).map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name} · {v.kind === "backup" ? "Backup vault" : "RSV"} · {v.location}
                  </option>
                ))}
              </select>
              {selectedVault && <div className="mt-1"><PortalLink resourceId={selectedVault.id} portalHost={portalHost} label="Open target vault" className="text-[11px]" /></div>}
            </label>
            <label className="text-sm">
              <span className="block text-xs text-gray-500">Backup policy</span>
              <select value={policyId} onChange={(e) => { setPolicyId(e.target.value); setPreview(null); }}
                disabled={!vaultId} className="mt-1 w-64 rounded-lg border px-2 py-1.5 disabled:bg-gray-100">
                <option value="">Select a policy…</option>
                {policies.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}{p.retention_days ? ` · ${p.retention_days}d` : ""}
                  </option>
                ))}
              </select>
              {selectedPolicy && <div className="mt-1"><PortalLink resourceId={selectedPolicy.arm_id || selectedPolicy.id} portalHost={portalHost} label="Open target policy" className="text-[11px]" /></div>}
            </label>
            <button onClick={() => previewM.mutate()} disabled={!selected.size || !vaultId || !policyId || previewM.isPending}
              className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40">
              {previewM.isPending ? "Validating…" : `Preview ${selected.size || ""}`}
            </button>
            <button onClick={() => submitM.mutate()} disabled={!preview?.some((i) => i.status === "ready") || submitM.isPending}
              className="rounded-lg bg-brand-dark px-3 py-1.5 text-sm text-white disabled:opacity-40">
              {submitM.isPending ? "Submitting…" : "Create change requests"}
            </button>
          </div>
          {preview && (
            <div className="mt-3 space-y-1 text-sm">
              {preview.map((item) => (
                <div key={item.gap_id} className={`rounded-lg p-2 ${item.status === "ready" ? "bg-emerald-50" : "bg-rose-50"}`}>
                  <span className="font-medium">{item.resource_name}</span>{" "}
                  <span className="text-xs text-gray-600">
                    {item.status === "ready" ? item.summary : item.reason}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {data.gaps.length === 0 ? (
        <Empty>Every eligible resource in this scope is protected.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="w-8 px-3 py-2">
                  <input type="checkbox"
                    checked={selected.size === data.gaps.length && data.gaps.length > 0}
                    onChange={(e) => setSelected(e.target.checked ? new Set(data.gaps.map((g) => g.gap_id)) : new Set())} />
                </th>
                <SortableTh label="Resource" column="resource" state={gapSorter.sort} onSort={gapSorter.update} className="px-3 py-2" />
                <SortableTh label="Type" column="type" state={gapSorter.sort} onSort={gapSorter.update} className="px-3 py-2" />
                <SortableTh label="Resource group" column="resource_group" state={gapSorter.sort} onSort={gapSorter.update} className="px-3 py-2" />
                <SortableTh label="Region" column="region" state={gapSorter.sort} onSort={gapSorter.update} className="px-3 py-2" />
                <SortableTh label="Severity" column="severity" state={gapSorter.sort} onSort={gapSorter.update}
                  first="desc" directionLabels={{ asc: "Lowest severity first", desc: "Highest severity first" }} className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {sortedGaps.map((gap: BackupGap) => (
                <tr key={gap.gap_id} className="hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <input type="checkbox" checked={selected.has(gap.gap_id)}
                      onChange={(e) => setSelected((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(gap.gap_id); else next.delete(gap.gap_id);
                        return next;
                      })} />
                  </td>
                  <td className="px-3 py-2 font-medium"><span className="inline-flex items-center gap-1">{gap.resource_name}<PortalLink resourceId={gap.resource_id} portalHost={portalHost} label="Open resource" /></span></td>
                  <td className="px-3 py-2 text-gray-600">{gap.display_type}</td>
                  <td className="px-3 py-2 text-gray-600">{gap.resource_group}</td>
                  <td className="px-3 py-2 text-gray-600">{gap.location}</td>
                  <td className="px-3 py-2">
                    <Pill tone={gap.severity === "critical" ? "bg-rose-100 text-rose-800" : "bg-amber-50 text-amber-700"}>
                      {gap.severity}
                    </Pill>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.native_only.length > 0 && (
        <div className="rounded-xl border bg-white p-4 text-xs text-gray-600">
          <div className="font-medium text-gray-800">Protected by service-native features (not vault-based)</div>
          <ul className="mt-1 list-disc pl-4">
            {data.native_only.map((item) => (
              <li key={item.type}><span className="font-mono">{item.type}</span> — {item.note}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- DR & drills
function DrTab({ snapshot, scope, caps, onBanner, portalHost }: {
  snapshot: BackupSnapshot; scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; portalHost: string;
}) {
  const qc = useQueryClient();
  // Drills live in the database and change without an analysis, so they stay a live query.
  const drillsQ = useQuery({ queryKey: queryKeys.backupManager.drills(scope), queryFn: () => api.backupManagerDrills(scope) });
  const [drillName, setDrillName] = useState("");

  const failoverM = useMutation({
    mutationFn: (itemId: string) => api.backupManagerTestFailover({ ...scope, replicated_item_id: itemId, network_type: "NoNetwork" }),
    onSuccess: (r) => {
      onBanner(`Test failover drafted as change ${r.change.id.slice(0, 8)} — it needs two approvers.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const cleanupM = useMutation({
    mutationFn: (itemId: string) => api.backupManagerTestFailoverCleanup({ ...scope, replicated_item_id: itemId }),
    onSuccess: (r) => {
      onBanner(`Cleanup drafted as change ${r.change.id.slice(0, 8)}.`);
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const createDrillM = useMutation({
    mutationFn: () => api.backupManagerCreateDrill({
      ...scope, name: drillName || "Recovery drill", kind: "restore",
      scope_kind: scope.workload_id ? "workload" : "subscription",
      scope_id: scope.workload_id || scope.subscription_id || "",
    }),
    onSuccess: () => { setDrillName(""); void qc.invalidateQueries({ queryKey: queryKeys.backupManager.drillsRoot }); },
    onError: (e: Error) => onBanner(e.message),
  });
  const outcomeM = useMutation({
    mutationFn: (vars: { id: string; status: "passed" | "failed" }) =>
      api.backupManagerDrillOutcome(vars.id, { status: vars.status, capture_evidence: true }),
    onSuccess: () => {
      onBanner("Drill recorded and evidence captured.");
      void qc.invalidateQueries({ queryKey: queryKeys.backupManager.drillsRoot });
    },
    onError: (e: Error) => onBanner(e.message),
  });

  const dr = snapshot.dr;
  const canDrill = caps?.can_drill && !caps?.read_only && !caps?.demo && !caps?.analysis_only_scope;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="RPO attainment" value={`${dr.rpo.attainment_pct}%`}
          hint={`${dr.rpo.breached} breached`} band={dr.rpo.breached ? "red" : "green"} />
        <Stat label="Replicated items" value={dr.summary.replicated_items}
          hint={`${dr.summary.unhealthy} unhealthy`} band={dr.summary.unhealthy ? "red" : "green"} />
        <Stat label="Stale drills" value={dr.summary.stale_drills}
          hint={`No test in ${dr.summary.drill_stale_days} days`} band={dr.summary.stale_drills ? "amber" : "green"} />
        <Stat label="Drill register" value={drillsQ.data?.summary.open ?? 0}
          hint={`${drillsQ.data?.summary.overdue ?? 0} overdue`} band={(drillsQ.data?.summary.overdue ?? 0) ? "amber" : undefined} />
      </div>

      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-semibold text-gray-900">Site Recovery replicated items</div>
        {dr.items.length === 0 ? (
          <div className="p-6 text-sm text-gray-500">No Site Recovery replication in this scope.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Item</th>
                <th className="px-3 py-2">Health</th>
                <th className="px-3 py-2">RPO</th>
                <th className="px-3 py-2">Last test failover</th>
                <th className="px-3 py-2">Issues</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {dr.items.map((item: BackupReplicationItem) => (
                <tr key={item.id}>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5 font-medium">{item.friendly_name}<PortalLink resourceId={item.id} portalHost={portalHost} label="Open replicated item" /><PortalLink resourceId={item.datasource_id} portalHost={portalHost} label="Open source resource" /><PortalLink resourceId={item.vault_id} portalHost={portalHost} label="Open vault" /></div>
                    <div className="text-[11px] text-gray-500">{item.primary_region} → {item.recovery_region}</div>
                  </td>
                  <td className="px-3 py-2"><Pill tone={BAND_STYLE[item.status]}>{item.replication_health}</Pill></td>
                  <td className="px-3 py-2">{item.rpo_seconds !== null ? `${Math.round(item.rpo_seconds / 60)}m` : "—"}</td>
                  <td className="px-3 py-2">
                    {item.last_test_failover_age_days === null ? (
                      <Pill tone="bg-rose-50 text-rose-700">never</Pill>
                    ) : `${Math.round(item.last_test_failover_age_days)}d ago`}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">{item.issues.join(" ") || "—"}</td>
                  <td className="px-3 py-2 text-right">
                    {canDrill && (
                      item.test_failover_active ? (
                        <button onClick={() => cleanupM.mutate(item.id)} disabled={cleanupM.isPending}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50">Clean up drill</button>
                      ) : (
                        <button onClick={() => failoverM.mutate(item.id)} disabled={failoverM.isPending}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                          title="Isolated (no-network) test failover, approval-gated">Run test failover</button>
                      )
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-semibold text-gray-900">Site Recovery plans</div>
        {dr.recovery_plans.length === 0 ? (
          <div className="p-6 text-sm text-gray-500">No Site Recovery plans in this scope.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Plan</th>
                <th className="px-3 py-2">Regions</th>
                <th className="px-3 py-2">Protected items</th>
                <th className="px-3 py-2">Last test failover</th>
                <th className="px-3 py-2">State</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {dr.recovery_plans.map((plan) => (
                <tr key={plan.id}>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5 font-medium">
                      {plan.friendly_name}
                      <PortalLink resourceId={plan.id} portalHost={portalHost} label="Open recovery plan" />
                      <PortalLink resourceId={plan.vault_id} portalHost={portalHost} label="Open vault" />
                    </div>
                    <div className="text-[11px] text-gray-500">{plan.current_scenario || "—"}</div>
                  </td>
                  <td className="px-3 py-2 text-gray-600">{plan.primary_region} → {plan.recovery_region}</td>
                  <td className="px-3 py-2">{plan.protected_item_count}</td>
                  <td className="px-3 py-2">
                    {plan.last_test_failover_age_days === null
                      ? <Pill tone="bg-rose-50 text-rose-700">never</Pill>
                      : `${Math.round(plan.last_test_failover_age_days)}d ago`}
                  </td>
                  <td className="px-3 py-2">
                    <Pill tone={plan.stale_drill ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}>
                      {plan.stale_drill ? "stale drill" : plan.current_scenario_status || "current"}
                    </Pill>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-xl border bg-white">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
          <span className="text-sm font-semibold text-gray-900">Recovery drill register</span>
          {canDrill && (
            <div className="flex items-center gap-2">
              <input value={drillName} onChange={(e) => setDrillName(e.target.value)}
                placeholder="Drill name (e.g. Quarterly restore test)"
                className="w-72 rounded-lg border px-2 py-1 text-sm" />
              <button onClick={() => createDrillM.mutate()} disabled={createDrillM.isPending}
                className="rounded-lg border px-2.5 py-1 text-sm hover:bg-gray-50">Schedule drill</button>
            </div>
          )}
        </div>
        {(drillsQ.data?.drills.length ?? 0) === 0 ? (
          <div className="p-6 text-sm text-gray-500">
            No drills recorded. A backup that has never been restored is an assumption, not a control.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Drill</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Due</th>
                <th className="px-3 py-2">RTO</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {drillsQ.data!.drills.map((drill) => (
                <tr key={drill.id}>
                  <td className="px-3 py-2 font-medium"><span className="inline-flex items-center gap-1">{drill.name}<PortalLink resourceId={drill.target_id} portalHost={portalHost} label="Open drill target" /></span></td>
                  <td className="px-3 py-2 text-gray-600">{drill.kind === "test_failover" ? "Test failover" : "Restore"}</td>
                  <td className="px-3 py-2">
                    <Pill tone={drill.status === "passed" ? STATUS_STYLE.succeeded : drill.status === "failed" ? STATUS_STYLE.failed : "bg-gray-100 text-gray-600"}>
                      {drill.status}
                    </Pill>
                  </td>
                  <td className="px-3 py-2">
                    {fmtDate(drill.due_at)}
                    {drill.overdue && <span className="ml-1 text-xs text-rose-600">overdue</span>}
                  </td>
                  <td className="px-3 py-2">{drill.rto_minutes !== null ? `${drill.rto_minutes}m` : "—"}</td>
                  <td className="px-3 py-2 text-right">
                    {canDrill && ["scheduled", "in_progress"].includes(drill.status) && (
                      <div className="flex justify-end gap-1">
                        <button onClick={() => outcomeM.mutate({ id: drill.id, status: "passed" })}
                          className="rounded border px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50">Passed</button>
                        <button onClick={() => outcomeM.mutate({ id: drill.id, status: "failed" })}
                          className="rounded border px-2 py-1 text-xs text-rose-700 hover:bg-rose-50">Failed</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- Cost
const RATE_SOURCE_LABEL: Record<string, string> = {
  azure_retail_prices: "Live Azure retail prices",
  reference: "Seeded reference prices",
  unavailable: "Prices unavailable",
};
const BASIS_LABEL: Record<string, string> = {
  consumed_gb: "measured consumption",
  estimated_cost: "estimated cost",
  equal: "equal share",
};

function CostTab({ snapshot, scope, portalHost }: { snapshot: BackupSnapshot; scope: BackupManagerScope; portalHost: string }) {
  const DEFAULT_MONTHS = 1;
  const DEFAULT_TYPE = "AmortizedCost" as const;
  const [monthsBack, setMonthsBack] = useState<number>(DEFAULT_MONTHS);
  const [costType, setCostType] = useState<"AmortizedCost" | "ActualCost">(DEFAULT_TYPE);
  // The analysis prices the default period. Asking for a different one is an explicit user
  // action, so that — and only that — is fetched on demand.
  const isDefaultPeriod = monthsBack === DEFAULT_MONTHS && costType === DEFAULT_TYPE;
  const opts = { monthsBack, costType };
  const q = useQuery({
    queryKey: queryKeys.backupManager.cost(scope, opts),
    queryFn: () => api.backupManagerCost(scope, opts),
    enabled: !isDefaultPeriod,
  });
  const data = isDefaultPeriod ? snapshot.cost : q.data;
  if (!isDefaultPeriod && q.isLoading) return <Empty>Pricing this period…</Empty>;
  if (!isDefaultPeriod && q.isError) return <Empty>{(q.error as Error).message}</Empty>;
  if (!data) return <Empty>No cost data in this analysis.</Empty>;
  const actuals = data.actuals;
  const variance = data.variance;
  const allocation = data.allocation;
  const allocated = new Map(allocation.rows.map((r) => [r.instance_id, r]));
  const headline = actuals.available && !actuals.mixed_currency ? actuals.total : data.monthly_total;
  const headlineCurrency = actuals.available ? actuals.currency : data.currency;
  const meters = Object.entries(actuals.by_meter ?? {});
  // Scale to the actual peak, not to a fixed 1-unit floor: a month that only ever spends
  // cents a day would otherwise draw as a flat line.
  const maxDaily = Math.max(Number.MIN_VALUE, ...(actuals.daily ?? []).map((d) => d.cost));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select value={monthsBack} onChange={(e) => setMonthsBack(Number(e.target.value))}
          className="rounded-lg border px-2 py-1.5 text-sm">
          <option value={1}>Last complete month</option>
          <option value={2}>Two months ago</option>
          <option value={3}>Three months ago</option>
          <option value={0}>This month to date</option>
        </select>
        <select value={costType} onChange={(e) => setCostType(e.target.value as "AmortizedCost" | "ActualCost")}
          className="rounded-lg border px-2 py-1.5 text-sm">
          <option value="AmortizedCost">Amortized (spreads reservations)</option>
          <option value="ActualCost">Actual (as invoiced)</option>
        </select>
        <span className="ml-auto text-xs text-gray-500">
          {actuals.available
            ? `Actual spend · ${actuals.period.from?.slice(0, 10)} → ${actuals.period.to?.slice(0, 10)}`
            : "Estimate only"}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label={actuals.available ? "Actual spend" : "Estimated monthly"}
          value={actuals.mixed_currency ? "Multiple currencies" : fmtMoney(headline, headlineCurrency)}
          hint={actuals.available ? `${costType === "AmortizedCost" ? "Amortized" : "Actual"} · from Cost Management` : data.confidence}
          band={actuals.available ? "green" : undefined} />
        <Stat label="List-price estimate" value={fmtMoney(data.monthly_total, data.currency)}
          hint={`${RATE_SOURCE_LABEL[data.rate_source] ?? data.rate_source}${data.region ? ` · ${data.region}` : ""}`} />
        <Stat label="Protected items" value={data.instance_count}
          hint={`${data.measured_instances} measured${data.unpriced_instances ? ` · ${data.unpriced_instances} unpriced` : ""}`} />
        <Stat label="Recoverable waste" value={fmtMoney(data.waste.recoverable_monthly, data.waste.currency)}
          hint={data.waste.basis === "actual" ? "from actual spend" : "estimated"}
          band={data.waste.recoverable_monthly > 0 ? "amber" : "green"} />
      </div>

      {actuals.mixed_currency && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <div className="font-medium">Actual spend uses multiple billing currencies</div>
          <div className="mt-1 flex flex-wrap gap-3">
            {Object.entries(actuals.totals_by_currency ?? {}).map(([currency, total]) => (
              <span key={currency}>{fmtMoney(total, currency)}</span>
            ))}
          </div>
          <p className="mt-1 text-xs">Amounts are not summed, allocated, or compared across currencies.</p>
        </div>
      )}

      {/* Estimate vs actual — the check that tells you whether the model is believable. */}
      <div className={`rounded-xl border p-4 ${variance.comparable ? "bg-white" : "border-dashed bg-gray-50"}`}>
        <h2 className="text-sm font-semibold text-gray-900">Estimate vs actual</h2>
        {variance.comparable ? (
          <div className="mt-2 flex flex-wrap items-baseline gap-4 text-sm">
            <span>List-price estimate <strong>{fmtMoney(variance.estimated, variance.estimate_currency)}</strong></span>
            <span>Actual <strong>{fmtMoney(variance.actual, variance.actual_currency)}</strong></span>
            <span className={variance.delta > 0 ? "text-rose-700" : "text-emerald-700"}>
              {variance.delta > 0 ? "over" : "under"} by {fmtMoney(Math.abs(variance.delta), variance.actual_currency)}
              {variance.delta_pct !== null && ` (${Math.abs(variance.delta_pct)}%)`}
            </span>
          </div>
        ) : (
          <p className="mt-1 text-sm text-gray-600">{variance.reason}</p>
        )}
        {!actuals.available && actuals.reason && (
          <p className="mt-2 text-xs text-amber-700">
            {actuals.reason} {actuals.remedy}
          </p>
        )}
        {actuals.available && actuals.partial && actuals.reason && (
          <p className="mt-2 text-xs text-amber-700">
            Partial Cost Management coverage: {actuals.reason}
          </p>
        )}
      </div>

      {meters.length > 0 && (
        <div className="rounded-xl border bg-white">
          <div className="border-b px-4 py-2 text-sm font-semibold text-gray-900">
            What you are actually billed for
          </div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-gray-500">
              <tr><th className="px-3 py-2">Meter</th><th className="px-3 py-2">Cost</th><th className="px-3 py-2">Share</th></tr>
            </thead>
            <tbody className="divide-y">
              {meters.map(([meter, amount]) => {
                const pct = actuals.total ? Math.round((100 * amount) / actuals.total) : 0;
                return (
                  <tr key={meter}>
                    <td className="px-3 py-2">{meter}</td>
                    <td className="px-3 py-2">{fmtMoney(amount, actuals.currency)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        <div className="h-2 w-32 overflow-hidden rounded-full bg-gray-100">
                          <div className="h-full bg-brand-dark" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="text-xs text-gray-500">{pct}%</span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {(actuals.daily ?? []).some((d) => d.cost > 0) && (
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Daily backup spend</h2>
          <div className="mt-2 flex h-24 items-end gap-0.5">
            {actuals.daily.map((point) => (
              <div key={point.date} className="flex-1"
                title={`${fmtUsageDate(point.date)}: ${fmtMoney(point.cost, actuals.currency)}`}>
                <div className="w-full rounded-t bg-brand-dark/70"
                  style={{ height: `${Math.max(2, Math.round((100 * point.cost) / maxDaily))}%` }} />
              </div>
            ))}
          </div>
          <div className="mt-1 flex justify-between text-[10px] text-gray-400">
            <span>{fmtUsageDate(actuals.daily[0]?.date ?? "")}</span>
            <span>{fmtUsageDate(actuals.daily[actuals.daily.length - 1]?.date ?? "")}</span>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-600">
        <div>
          <strong>{RATE_SOURCE_LABEL[data.rate_source] ?? data.rate_source}</strong>
          {data.region && ` · ${data.region}`} · {data.currency}
          {data.rate_source === "azure_retail_prices"
            ? " — list prices from the Azure Retail Prices API, refreshed automatically."
            : ` — ${data.source}`}
          {data.rate_error && <span className="ml-1 text-amber-700">{data.rate_error}</span>}
        </div>
        {allocation.rows.length > 0 && <div className="mt-1">{allocation.note}</div>}
        {allocation.unattributed_total > 0 && (
          <div className="mt-1 text-amber-700">
            {fmtMoney(allocation.unattributed_total, allocation.currency)} of spend belongs to
            {" "}{allocation.vaults_unattributed} vault(s) outside this scope and is not shown per item.
          </div>
        )}
        {data.report_note && <div className="mt-1 text-amber-700">{data.report_note}</div>}
      </div>

      {data.waste.findings.length > 0 && (
        <div className="rounded-xl border bg-white">
          <div className="flex items-center justify-between border-b px-4 py-2">
            <span className="text-sm font-semibold text-gray-900">Waste</span>
            <Pill tone={data.waste.basis === "actual" ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-600"}>
              {data.waste.basis === "actual" ? "priced from actual spend" : "estimated"}
            </Pill>
          </div>
          <ul className="divide-y">
            {data.waste.findings.map((finding, index) => (
              <li key={`${finding.kind}-${finding.instance_id}-${index}`} className="flex items-start justify-between gap-3 px-4 py-3">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900">{finding.title}</span>
                    <Pill tone={finding.severity === "error" ? "bg-rose-50 text-rose-700" : finding.severity === "warning" ? "bg-amber-50 text-amber-700" : "bg-gray-100 text-gray-600"}>
                      {finding.severity}
                    </Pill>
                  </div>
                  <div className="flex items-center gap-1 text-xs text-gray-600">
                    <span>{finding.name} · {finding.detail}</span>
                    {finding.kind !== "orphaned_protection" && <PortalLink resourceId={finding.datasource_id} portalHost={portalHost} label="Open source resource" />}
                    <PortalLink resourceId={finding.instance_id} portalHost={portalHost} label="Open waste target" />
                    <PortalLink resourceId={finding.vault_id} portalHost={portalHost} label="Open vault" />
                  </div>
                  <div className="mt-0.5 text-xs text-gray-500">{finding.action}</div>
                </div>
                <div className="whitespace-nowrap text-sm font-medium text-gray-900">
                  {finding.monthly_cost ? `${fmtMoney(finding.monthly_cost, data.waste.currency)}/mo` : ""}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="overflow-x-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Item</th>
              <th className="px-3 py-2">Vault</th>
              <th className="px-3 py-2">Meter</th>
              <th className="px-3 py-2">Stored</th>
              <th className="px-3 py-2">Estimated</th>
              {allocated.size > 0 && <th className="px-3 py-2">Actual (allocated)</th>}
            </tr>
          </thead>
          <tbody className="divide-y">
            {data.top_rows.map((row) => {
              const share = allocated.get(row.instance_id);
              return (
                <tr key={row.instance_id}>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5 font-medium">{row.name}<PortalLink resourceId={row.datasource_id} portalHost={portalHost} label="Open source resource" /><PortalLink resourceId={row.instance_id} portalHost={portalHost} label="Open protected item" /></div>
                    {row.note && <div className="text-[11px] text-gray-500">{row.note}</div>}
                  </td>
                  <td className="px-3 py-2 text-gray-600">
                    <span className="inline-flex items-center gap-1">{row.vault_name}<PortalLink resourceId={row.vault_id} portalHost={portalHost} label="Open vault" /></span>
                    <div className="text-[11px] text-gray-400">{row.redundancy || "—"}</div>
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">{row.meter || "—"}</td>
                  <td className="px-3 py-2">
                    {row.stored_gb} GB {!row.measured && <span className="text-[10px] text-gray-400">est</span>}
                  </td>
                  <td className="px-3 py-2">{fmtMoney(row.monthly_cost, data.currency)}</td>
                  {allocated.size > 0 && (
                    <td className="px-3 py-2">
                      {share ? (
                        <>
                          {fmtMoney(share.allocated_cost, allocation.currency)}
                          <div className="text-[10px] text-gray-400">by {BASIS_LABEL[share.weight_basis]}</div>
                        </>
                      ) : "—"}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- Changes
function ChangesTab({ scope, caps, onBanner, portalHost }: { scope: BackupManagerScope; caps: any; onBanner: (m: string) => void; portalHost: string }) {
  const qc = useQueryClient();
  const connectionId = scope.connection_id ?? "";
  const [page, setPage] = useState(1);
  const [view, setView] = useState<"all" | "action_required">("action_required");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("Reviewed and approved");
  const q = useQuery({
    queryKey: queryKeys.backupManager.changes(connectionId, page, 100, view, ""),
    queryFn: () => api.backupManagerChanges(connectionId, page, 100, view, ""),
    refetchInterval: (query) => ((query.state.data?.applying_count ?? 0) > 0 ? 8_000 : false),
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: queryKeys.backupManager.changesRoot });
    void qc.invalidateQueries({ queryKey: queryKeys.backupManager.summaryRoot });
  };
  // Replace the rows we just acted on in the current page immediately, so the table reflects
  // the decision before the authoritative refetch lands (an apply round-trips to Azure).
  const patchRows = (updated: BackupManagerChange[]) => {
    if (!updated.length) return;
    const byId = new Map(updated.map((c) => [c.id, c]));
    qc.setQueryData<BackupChangesResp>(
      queryKeys.backupManager.changes(connectionId, page, 100, view, ""),
      (previous) => {
        if (!previous) return previous;
        const rows = previous.rows.map((row) => byId.get(row.id) ?? row);
        return {
          ...previous,
          rows: view === "action_required"
            ? rows.filter((row) => row.status === "pending" || row.status === "approved")
            : rows,
        };
      },
    );
  };
  const decideM = useMutation({
    mutationFn: (vars: { decision: "approved" | "rejected" }) =>
      api.backupManagerBulkDecide(connectionId, Array.from(selected), vars.decision, reason),
    onSuccess: (r) => {
      onBanner(`${r.updated.length} change(s) ${r.updated[0]?.status ?? "updated"}${r.skipped.length ? `; ${r.skipped.length} skipped` : ""}.`);
      setSelected(new Set());
      patchRows(r.updated);
      invalidate();
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const singleDecideM = useMutation({
    mutationFn: (vars: { id: string; decision: "approved" | "rejected" }) =>
      api.backupManagerDecideChange(vars.id, vars.decision, reason),
    onSuccess: (r) => {
      onBanner(r.awaiting_second_approver
        ? "First approval recorded — a second, different approver is required."
        : `Change ${r.change.status}.`);
      patchRows([r.change]);
      invalidate();
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const applyM = useMutation({
    mutationFn: () => api.backupManagerBulkApply(connectionId, Array.from(selected)),
    onSuccess: (r) => {
      const applying = r.results.filter((c) => c.status === "applying").length;
      onBanner(applying
        ? `${applying} change(s) submitted to Azure — they complete asynchronously and this list updates itself.`
        : `${r.results.length} change(s) processed.`);
      setSelected(new Set());
      patchRows(r.results);
      invalidate();
    },
    onError: (e: Error) => onBanner(e.message),
  });
  const rollbackM = useMutation({
    mutationFn: (id: string) => api.backupManagerRollback(id),
    onSuccess: () => { onBanner("Rollback drafted as a new pending change."); invalidate(); },
    onError: (e: Error) => onBanner(e.message),
  });

  if (caps?.demo) return <Empty>Demo mode does not create managed changes.</Empty>;
  if (q.isLoading) return <Empty>Loading managed changes…</Empty>;
  if (q.isError) return <Empty>{(q.error as Error).message}</Empty>;
  const data = q.data!;
  const canApprove = caps?.can_approve && !caps?.read_only && !caps?.analysis_only_scope;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="Pending" value={data.pending_count} band={data.pending_count ? "amber" : undefined} />
        <Stat label="Approved, not applied" value={data.approved_count} band={data.approved_count ? "amber" : undefined} />
        <Stat label="Applying" value={data.applying_count} hint="Azure long-running operations" />
        <Stat label="Total" value={data.total_count} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select value={view} onChange={(e) => { setView(e.target.value as "all" | "action_required"); setPage(1); }}
          className="rounded-lg border px-2 py-1.5 text-sm">
          <option value="action_required">Action required</option>
          <option value="all">All changes</option>
        </select>
        {canApprove && (
          <>
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Decision reason"
              className="w-64 rounded-lg border px-3 py-1.5 text-sm" />
            <button onClick={() => decideM.mutate({ decision: "approved" })}
              disabled={!selected.size || !reason.trim() || decideM.isPending}
              className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40">Approve</button>
            <button onClick={() => decideM.mutate({ decision: "rejected" })}
              disabled={!selected.size || !reason.trim() || decideM.isPending}
              className="rounded-lg border px-3 py-1.5 text-sm text-rose-700 disabled:opacity-40">Reject</button>
            <button onClick={() => applyM.mutate()} disabled={!selected.size || applyM.isPending}
              className="rounded-lg bg-brand-dark px-3 py-1.5 text-sm text-white disabled:opacity-40">
              {applyM.isPending ? "Applying…" : "Apply to Azure"}
            </button>
          </>
        )}
        <span className="ml-auto text-xs text-gray-500">{selected.size} selected</span>
      </div>

      {data.rows.length === 0 ? (
        <Empty>No managed changes {view === "action_required" ? "await action" : "recorded"}.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="w-8 px-3 py-2">
                  <input type="checkbox" checked={selected.size === data.rows.length && data.rows.length > 0}
                    onChange={(e) => setSelected(e.target.checked ? new Set(data.rows.map((r) => r.id)) : new Set())} />
                </th>
                <th className="px-3 py-2">Change</th>
                <th className="px-3 py-2">Target</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Risk</th>
                <th className="px-3 py-2">Requested</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.rows.map((change: BackupManagerChange) => (
                <tr key={change.id} className="hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <input type="checkbox" checked={selected.has(change.id)}
                      onChange={(e) => setSelected((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(change.id); else next.delete(change.id);
                        return next;
                      })} />
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-medium">{String(change.summary.description ?? change.target_label)}</div>
                    <div className="text-[11px] text-gray-500">{change.target_label} · {change.operation}</div>
                    {change.requires_dual_approval && (
                      <Pill tone="bg-indigo-50 text-indigo-700">
                        {change.second_approver ? "two approvers recorded" : "needs two approvers"}
                      </Pill>
                    )}
                  </td>
                  <td className="max-w-xs truncate px-3 py-2 text-xs text-gray-600" title={change.target_id}>
                    <span className="inline-flex items-center gap-1">{change.target_name}<PortalLink resourceId={change.target_id} portalHost={portalHost} label="Open change target" /></span>
                  </td>
                  <td className="px-3 py-2">
                    <Pill tone={CHANGE_STATUS_STYLE[change.status] ?? "bg-gray-100 text-gray-600"}>{change.status}</Pill>
                    {change.status === "applying" && (
                      <div className="text-[10px] text-sky-600">polled {change.poll_attempts}×</div>
                    )}
                    {change.error_message && (
                      <div className="mt-0.5 max-w-xs text-[11px] text-rose-700">{change.error_message}</div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <Pill tone={change.risk === "high" ? "bg-rose-50 text-rose-700" : change.risk === "medium" ? "bg-amber-50 text-amber-700" : "bg-gray-100 text-gray-600"}>
                      {change.risk}
                    </Pill>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">
                    {fmtDate(change.requested_at)}<br />{change.requested_by}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {canApprove && change.requires_dual_approval && change.status === "pending" && (
                      <button onClick={() => singleDecideM.mutate({ id: change.id, decision: "approved" })}
                        className="rounded border px-2 py-1 text-xs hover:bg-gray-50">Approve</button>
                    )}
                    {canApprove && change.can_rollback && (
                      <button onClick={() => rollbackM.mutate(change.id)} disabled={rollbackM.isPending}
                        className="rounded border px-2 py-1 text-xs hover:bg-gray-50">Roll back</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.total_count > data.page_size && (
        <div className="flex items-center justify-between text-sm">
          <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}
            className="rounded-lg border px-3 py-1.5 disabled:opacity-40">← Previous</button>
          <span className="text-xs text-gray-500">
            {(page - 1) * data.page_size + 1}–{Math.min(page * data.page_size, data.total_count)} of {data.total_count}
          </span>
          <button disabled={!data.has_more} onClick={() => setPage((p) => p + 1)}
            className="rounded-lg border px-3 py-1.5 disabled:opacity-40">Next →</button>
        </div>
      )}
    </div>
  );
}
