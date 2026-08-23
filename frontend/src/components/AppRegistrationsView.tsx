import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, streamAppRegistrationsRefresh, type AppRegProgress, type AppRegRefreshMode, type AppRegistration, type AppRegistrationsResponse, type EnterpriseAppState } from "../api";import { formatError } from "../utils/format";
import { Skeleton, useDebounced, VirtualList } from "../utils/perf";
import { cmp, SortScopeNote, useEntraSorted, useSortState, type SortDir, type SortState } from "./entra/EntraShared";

function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const AUDIENCE_LABEL: Record<string, string> = {
  AzureADMyOrg: "Single tenant",
  AzureADMultipleOrgs: "Multi-tenant",
  AzureADandPersonalMicrosoftAccount: "Multi-tenant + personal",
  PersonalMicrosoftAccount: "Personal accounts",
};

/** Deep-link to an app registration's Overview blade in the Azure portal (keyed by appId). */
function portalUrl(a: AppRegistration): string {
  return `https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/Overview/appId/${encodeURIComponent(a.appId)}/isMSAApp~/false`;
}

type CredFilter = "secrets" | "certs" | "expiring" | "expired" | "none";

const ENTERPRISE_STATES: EnterpriseAppState[] = ["active", "deactivated", "not_instantiated", "unknown"];
const ENTERPRISE_STATE_META: Record<EnterpriseAppState, { label: string; cls: string; title: string }> = {
  active: { label: "Active", cls: "bg-green-100 text-green-700", title: "The local enterprise application is enabled." },
  deactivated: { label: "Deactivated", cls: "bg-red-100 text-red-700", title: "The local enterprise application is disabled." },
  not_instantiated: { label: "No local enterprise app", cls: "bg-gray-100 text-gray-600", title: "No corresponding service principal exists in this tenant." },
  unknown: { label: "Unknown", cls: "bg-amber-100 text-amber-700", title: "The enterprise-application state could not be determined." },
};

/** Columns the grid can sort by, in header order. */
type AppRegSortKey =
  | "name" | "audience" | "state" | "secrets" | "certs"
  | "appPerms" | "delegated" | "nextExpiry" | "lastSignIn" | "lastFailed" | "owners" | "risk";

/**
 * One definition of the column track, used by the header and the rows.
 *
 * They must stay identical or the header labels stop lining up with their data. Widths were
 * rebalanced when the headers became sortable: a sort arrow costs each label ~10px, which
 * was enough to truncate "Secrets", "App perms", "Delegated" and "Last sign-in".
 * Written out in full so Tailwind's scanner sees the literal class.
 */
const GRID_COLS =
  "grid-cols-[1.65fr_0.7fr_0.8fr_0.6fr_0.55fr_0.75fr_0.75fr_0.9fr_0.95fr_0.95fr_0.9fr_0.55fr]";

/** Most-interesting first when descending, per the Entra grid convention. */
const ENTERPRISE_STATE_RANK: Record<string, number> = {
  deactivated: 3, unknown: 2, not_instantiated: 1, active: 0,
};

/**
 * Sort value for the sign-in column.
 *
 * Three facts share this column and must not collapse into one. An unreadable report is
 * genuinely unknown, so it is null and the null rule pins it to the bottom. But "measured,
 * and it never signed in" is NOT unknown — it is the staleness extreme, and it is exactly
 * the row a dormancy review is hunting for. Ranking it below every real date keeps it
 * reachable by sorting instead of stranding it with the unmeasured rows.
 */
function signInSortValue(a: AppRegistration): number | null {
  if (!a.lastSignInKnown) return null;
  if (!a.lastSignIn) return 0;
  const t = new Date(a.lastSignIn).getTime();
  return Number.isNaN(t) ? null : t;
}

function compareAppRegs(a: AppRegistration, b: AppRegistration, key: AppRegSortKey): number {
  switch (key) {
    case "audience":
      return cmp.text(AUDIENCE_LABEL[a.signInAudience] ?? a.signInAudience,
                      AUDIENCE_LABEL[b.signInAudience] ?? b.signInAudience);
    case "state": return cmp.rank(ENTERPRISE_STATE_RANK, a.enterpriseAppState, b.enterpriseAppState);
    case "secrets": return cmp.num(a.secretsCount, b.secretsCount);
    case "certs": return cmp.num(a.certsCount, b.certsCount);
    case "appPerms": return cmp.num(a.applicationPermissionsCount, b.applicationPermissionsCount);
    case "delegated": return cmp.num(a.delegatedPermissionsCount, b.delegatedPermissionsCount);
    case "nextExpiry": return cmp.num(a.nextExpiryDays, b.nextExpiryDays);
    case "lastSignIn": return cmp.num(signInSortValue(a), signInSortValue(b));
    case "lastFailed": return cmp.date(a.lastFailedSignIn, b.lastFailedSignIn);
    case "owners":
      return cmp.text(a.ownerless ? "" : a.owners.join(", "),
                      b.ownerless ? "" : b.owners.join(", "));
    case "risk": return cmp.bool(a.highRisk, b.highRisk);
    default: return cmp.text(a.displayName, b.displayName);
  }
}

/**
 * A sortable header cell for this CSS-grid layout.
 *
 * `SortTh` in EntraShared renders a `<th>`, and this grid has no table to put one in. The
 * behaviour — first-click direction, arrow glyphs, toggle — is deliberately identical, so
 * the two grids feel the same. No `aria-sort`/`columnheader`: those are only meaningful
 * inside a table/grid role, and claiming them here would mislead a screen reader.
 */
function SortHead({ label, col, sort, setSort, align = "left", firstDir = -1, title }: {
  label: string;
  col: AppRegSortKey;
  sort: SortState<AppRegSortKey>;
  setSort: (s: SortState<AppRegSortKey>) => void;
  align?: "left" | "center";
  firstDir?: SortDir;
  title?: string;
}) {
  const active = sort.key === col;
  const state = active ? (sort.dir === -1 ? "sorted descending" : "sorted ascending") : "not sorted";
  return (
    <button
      type="button"
      data-testid={`appregs-sort-${col}`}
      onClick={() => setSort({ key: col, dir: active ? ((sort.dir * -1) as SortDir) : firstDir })}
      title={title || `Sort by ${label.toLowerCase()}`}
      aria-label={`${label}, ${state}. Activate to sort.`}
      className={`flex min-w-0 items-center hover:text-gray-800 ${
        align === "center" ? "justify-center" : ""} ${active ? "text-gray-800" : ""}`}
    >
      <span className="truncate uppercase tracking-wide">{label}</span>
      <span className={`ml-0.5 shrink-0 text-[9px] leading-none ${active ? "text-brand" : "text-gray-300"}`}>
        {active ? (sort.dir === -1 ? "▼" : "▲") : "↕"}
      </span>
    </button>
  );
}

function EnterpriseStateBadge({ state }: { state?: EnterpriseAppState }) {
  const meta = ENTERPRISE_STATE_META[state ?? "unknown"];
  return (
    <span
      data-testid={`appregs-state-${state ?? "unknown"}`}
      title={meta.title}
      className={`inline-flex max-w-full truncate rounded px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}
    >
      {meta.label}
    </span>
  );
}

const RISK_CLS: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-amber-100 text-amber-700",
  low: "bg-gray-100 text-gray-600",
};

function ExpiryBadge({ days }: { days: number | null }) {
  if (days == null) return <span className="text-gray-300">—</span>;
  const expired = days < 0;
  const cls = expired
    ? "bg-red-100 text-red-700"
    : days <= 30
    ? "bg-orange-100 text-orange-700"
    : days <= 60
    ? "bg-amber-100 text-amber-700"
    : "bg-sky-100 text-sky-700";
  return (
    <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>
      {expired ? `expired ${Math.abs(days)}d` : `${days}d`}
    </span>
  );
}

// Mirrors appregs.signin_bucket on the backend — the facet values must match exactly.
const SIGNIN_BUCKETS = ["Last 7 days", "8-30 days", "Over 30 days", "Attempted, never succeeded", "No sign-in in 30 days", "Not measured"] as const;
type SignInBucket = (typeof SIGNIN_BUCKETS)[number];
// A rejected attempt is not usage, so the dormancy KPI counts it too.
const DORMANT_BUCKETS: SignInBucket[] = ["Over 30 days", "Attempted, never succeeded", "No sign-in in 30 days"];

function signinBucket(a: AppRegistration): SignInBucket {
  if (!a.lastSignInKnown) return "Not measured";
  if (a.lastSignInDays == null) {
    return a.lastAttempt ? "Attempted, never succeeded" : "No sign-in in 30 days";
  }
  if (a.lastSignInDays <= 7) return "Last 7 days";
  if (a.lastSignInDays <= 30) return "8-30 days";
  return "Over 30 days";
}

function daysAgoText(days: number): string {
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days}d ago`;
}

/** Last sign-in for one app. Renders four DIFFERENT things, because they are four different
 *  facts: a successful date, a rejected attempt, "measured but nothing signed in", and "we
 *  could not look". Showing an attempt as a sign-in makes a broken integration look live. */
function LastSignInCell({ a, windowDays }: { a: AppRegistration; windowDays: number }) {
  if (!a.lastSignInKnown) {
    return (
      <span
        data-testid="appregs-signin-unmeasured"
        title="Sign-in activity could not be read for this tenant. This is NOT a statement that the application is unused."
        className="whitespace-nowrap rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500"
      >
        not measured
      </span>
    );
  }
  if (!a.lastSignIn || a.lastSignInDays == null) {
    // Something tried and was turned away. Neither "in use" nor "dormant" — a third answer.
    if (a.lastAttempt && a.lastAttemptDays != null) {
      return (
        <span
          data-testid="appregs-signin-failed"
          title={`Last attempt ${new Date(a.lastAttempt).toLocaleString()} — it did not succeed.\nMicrosoft's report counts a rejected credential as sign-in activity, so a date here is an attempt, not use.\nNo successful sign-in in the last ${windowDays} days.`}
          className="whitespace-nowrap rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700"
        >
          failed {daysAgoText(a.lastAttemptDays)}
        </span>
      );
    }
    return (
      <span
        data-testid="appregs-signin-none"
        title={`Nothing signed into this application in the last ${windowDays} days. Microsoft's report does not go back further, so this is not proof it was never used.`}
        className="whitespace-nowrap rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
      >
        none in {windowDays}d
      </span>
    );
  }
  const stale = a.lastSignInDays > 30;
  return (
    <span
      data-testid="appregs-signin-date"
      title={`${new Date(a.lastSignIn).toLocaleString()}${a.lastSignInApplication ? "\napp-only (client credentials)" : ""}${a.lastSignInDelegated ? "\ndelegated (on behalf of a user)" : ""}`}
      className={`whitespace-nowrap tabular-nums text-xs ${stale ? "text-amber-700" : "text-gray-700"}`}
    >
      {daysAgoText(a.lastSignInDays)}
    </span>
  );
}

/** The most recent rejected sign-in.
 *
 *  Three states, and the middle one is the point: Microsoft's per-application report does not
 *  separate success from failure, and the per-event logs that do need an Entra ID P1 licence.
 *  Where that is the case the column must say so, because an empty column reads as
 *  "nothing is failing". */
function LastFailedCell({ a, measured, reason }: {
  a: AppRegistration; measured: boolean; reason: string;
}) {
  if (!measured) {
    return (
      <span
        data-testid="appregs-lastfailed-unmeasured"
        title={reason || "Sign-in failures cannot be read for this tenant."}
        className="whitespace-nowrap text-[10px] text-gray-400"
      >
        not measured
      </span>
    );
  }
  // The per-event read is bounded, so the tenant-level "measured" does not mean THIS
  // application was covered. Without the per-app flag an unread one renders as a dash,
  // which reads as "nothing failed" — the opposite of what is known.
  if (!a.lastFailedSignInKnown) {
    return (
      <span
        data-testid="appregs-lastfailed-pending"
        title="Not read yet. Sign-in outcomes are collected a batch at a time; refresh again to extend the coverage."
        className="whitespace-nowrap text-[10px] text-amber-600"
      >
        not read yet
      </span>
    );
  }
  if (!a.lastFailedSignIn || a.lastFailedSignInDays == null) {
    return <span className="text-[10px] text-gray-300" title="No sign-in failure in the reported window.">—</span>;
  }
  return (
    <span
      data-testid="appregs-lastfailed"
      title={`${new Date(a.lastFailedSignIn).toLocaleString()}\nThis attempt was stamped after the last successful sign-in, so it did not succeed.`}
      className="whitespace-nowrap rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600"
    >
      {daysAgoText(a.lastFailedSignInDays)}
    </span>
  );
}

/** Last use of one credential — the answer to "which of these secrets can I retire?". */function LastUsedBadge({ lastUsed, known, days }: { lastUsed: string | null; known: boolean; days: number | null }) {
  if (!known) return <span className="text-[10px] text-gray-400" title="Per-credential usage could not be read.">usage not measured</span>;
  if (!lastUsed || days == null) return <span className="text-[10px] text-amber-700" title="Not used inside the reported window.">not used recently</span>;
  return <span className="text-[10px] text-gray-500" title={new Date(lastUsed).toLocaleString()}>used {daysAgoText(days)}</span>;
}

function FacetGroup({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-gray-100 py-2">
      <button onClick={() => setOpen((o) => !o)} className="mb-1 flex w-full items-center gap-1 px-1 text-left text-[11px] font-semibold uppercase tracking-wide text-gray-500">
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
        {title}
      </button>
      {open && <div className="space-y-0.5">{children}</div>}
    </div>
  );
}

function FacetRow({ label, count, active, onClick }: { label: string; count?: number; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center justify-between rounded px-2 py-1 text-left text-xs transition ${
        active ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-100"
      }`}
    >
      <span className="truncate">{label}</span>
      {count != null && <span className="ml-2 shrink-0 text-[10px] text-gray-400">{count}</span>}
    </button>
  );
}

function Kpi({ label, value, tone, active, onClick }: { label: string; value: number; tone?: string; active?: boolean; onClick?: () => void }) {
  const base = `rounded-lg border bg-white px-3 py-2 text-left transition ${active ? "ring-2 ring-brand border-brand" : ""}`;
  const inner = (<><div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div><div className="truncate text-[11px] text-gray-500">{label}</div></>);
  if (!onClick) return <div className={base}>{inner}</div>;
  return <button type="button" onClick={onClick} className={`${base} hover:border-brand hover:shadow-sm`} title={active ? "Click to clear filter" : `Filter to ${label}`}>{inner}</button>;
}

export function AppRegistrationsView({ connectionId = null }: { connectionId?: string | null }) {
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Live progress log for the (slow, background) refresh. Each entry is one streamed step.
  const [progress, setProgress] = useState<AppRegProgress[]>([]);
  const [showProgress, setShowProgress] = useState(false);
  const [refreshMode, setRefreshMode] = useState<AppRegRefreshMode>("capped");
  const [cancelling, setCancelling] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Filters
  const [text, setText] = useState("");
  const dText = useDebounced(text, 150);
  const [audSel, setAudSel] = useState<Set<string>>(new Set());
  const [permTypeSel, setPermTypeSel] = useState<Set<"Application" | "Delegated">>(new Set());
  const [credSel, setCredSel] = useState<Set<CredFilter>>(new Set());
  const [stateSel, setStateSel] = useState<Set<EnterpriseAppState>>(new Set());
  const [highRiskOnly, setHighRiskOnly] = useState(false);
  const [permSel, setPermSel] = useState<Set<string>>(new Set());
  const [ownerSel, setOwnerSel] = useState<Set<string>>(new Set());
  const [signinSel, setSigninSel] = useState<Set<SignInBucket>>(new Set());
  const [permSearch, setPermSearch] = useState("");

  const q = useQuery({
    queryKey: ["appRegistrations", connectionId],
    queryFn: () => api.appRegistrations(connectionId),
    staleTime: Infinity,
    retry: false,
  });
  const data: AppRegistrationsResponse | undefined = q.data;

  // Attach to the SSE progress stream. The server job runs in the background and survives
  // disconnects, so this both LAUNCHES (when none running) and FOLLOWS the refresh.
  const followStream = useCallback((mode: AppRegRefreshMode) => {
    setRefreshMode(mode);
    setRefreshing(true);
    setCancelling(false);
    setShowProgress(true);
    setProgress([]);
    setMsg(null);
    void streamAppRegistrationsRefresh(
      {
        onStart: (job) => setRefreshMode(job.mode),
        onProgress: (p) => setProgress((prev) => [...prev, p]),
        onDone: (fresh) => {
          qc.setQueryData(["appRegistrations", connectionId], fresh);
          setRefreshing(false);
          setCancelling(false);
          setMsg({ text: `Refreshed — ${fresh.summary?.total ?? 0} app registration(s).`, ok: true });
        },
        onError: (m) => {
          setRefreshing(false);
          setCancelling(false);
          setMsg({ text: m, ok: false });
        },
        onCancelled: (d) => {
          setRefreshing(false);
          setCancelling(false);
          setMsg({ text: `${d.message}${d.resume_available ? " Press Refresh to resume from the last completed page." : ""}`, ok: false });
        },
      },
      connectionId,
      mode,
    ).catch((e) => {
      setRefreshing(false);
      setCancelling(false);
      setMsg({ text: formatError(e), ok: false });
    });
  }, [qc, connectionId]);

  function doRefresh() {
    followStream(refreshMode);
  }

  async function cancelRefresh() {
    setCancelling(true);
    try {
      await api.cancelAppRegistrationsRefresh(connectionId);
    } catch (e) {
      setCancelling(false);
      setMsg({ text: formatError(e), ok: false });
    }
  }

  // On mount: if a background refresh is already running (e.g. started on another tab or
  // before navigating away), re-attach to its live progress automatically.
  useEffect(() => {
    let cancelled = false;
    void api
      .appRegistrationsJob(connectionId)
      .then((r) => {
        if (!cancelled && r.job && (r.job.status === "running" || r.job.status === "cancelling" || r.job.status === "paused")) {
          followStream(r.job.mode);
        }
      })
      .catch(() => {
        /* ignore — no job yet */
      });
    return () => {
      cancelled = true;
    };
  }, [followStream]);

  const liveProgress = useMemo(
    () => [...progress].reverse().find((entry) => entry.current != null || entry.page != null),
    [progress],
  );
  const progressPercent = liveProgress?.percent ?? (
    liveProgress?.current != null && liveProgress.total
      ? Math.min(100, (liveProgress.current / liveProgress.total) * 100)
      : null
  );

  // Keep the progress log scrolled to the newest line.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [progress]);

  function toggle<T>(set: Set<T>, v: T, setter: (s: Set<T>) => void) {
    const n = new Set(set);
    n.has(v) ? n.delete(v) : n.add(v);
    setter(n);
  }

  const apps = data?.apps ?? [];

  function matches(a: AppRegistration): boolean {
    if (audSel.size && !audSel.has(a.signInAudience)) return false;
    if (stateSel.size && !stateSel.has(a.enterpriseAppState ?? "unknown")) return false;
    if (permTypeSel.size) {
      const hasApp = a.applicationPermissionsCount > 0;
      const hasDel = a.delegatedPermissionsCount > 0;
      if (permTypeSel.has("Application") && !hasApp) return false;
      if (permTypeSel.has("Delegated") && !hasDel) return false;
    }
    if (credSel.size) {
      for (const c of credSel) {
        if (c === "secrets" && a.secretsCount === 0) return false;
        if (c === "certs" && a.certsCount === 0) return false;
        if (c === "expiring" && !(a.nextExpiryDays != null && a.nextExpiryDays >= 0 && a.nextExpiryDays <= 30)) return false;
        if (c === "expired" && a.expiredCredentials === 0) return false;
        if (c === "none" && (a.secretsCount > 0 || a.certsCount > 0)) return false;
      }
    }
    if (highRiskOnly && !a.highRisk) return false;
    if (permSel.size) {
      const vals = new Set(a.permissions.map((p) => p.value));
      for (const p of permSel) if (!vals.has(p)) return false;
    }
    if (ownerSel.size) {
      const owners = a.ownerless ? new Set(["(ownerless)"]) : new Set(a.owners);
      let ok = false;
      for (const o of ownerSel) if (owners.has(o)) ok = true;
      if (!ok) return false;
    }
    if (signinSel.size && !signinSel.has(signinBucket(a))) return false;
    const t = dText.trim().toLowerCase();
    if (t) {
      const state = ENTERPRISE_STATE_META[a.enterpriseAppState ?? "unknown"].label;
      const hay = `${a.displayName} ${a.appId} ${a.publisherDomain} ${a.tags.join(" ")} ${a.owners.join(" ")} ${state} ${a.servicePrincipalType ?? ""} ${a.disabledByMicrosoftStatus ?? ""} ${signinBucket(a)}`.toLowerCase();
      if (!hay.includes(t)) return false;
    }
    return true;
  }

  const filtered = useMemo(() => apps.filter(matches), [apps, audSel, stateSel, permTypeSel, credSel, highRiskOnly, permSel, ownerSel, signinSel, dText]);

  // Default mirrors the order the server already sends, so the first paint is unchanged.
  const [sort, setSort] = useSortState<AppRegSortKey>("app-registrations", { key: "name", dir: 1 });
  const sorted = useEntraSorted(filtered, sort, compareAppRegs);

  // Counts for the fixed facet rows (computed over the full app set, like the other facets).
  const facetCounts = useMemo(() => {
    let application = 0, delegated = 0, secrets = 0, certs = 0, expiring = 0, expired = 0, none = 0, highRisk = 0;
    for (const a of apps) {
      if (a.applicationPermissionsCount > 0) application++;
      if (a.delegatedPermissionsCount > 0) delegated++;
      if (a.secretsCount > 0) secrets++;
      if (a.certsCount > 0) certs++;
      if (a.nextExpiryDays != null && a.nextExpiryDays >= 0 && a.nextExpiryDays <= 30) expiring++;
      if (a.expiredCredentials > 0) expired++;
      if (a.secretsCount === 0 && a.certsCount === 0) none++;
      if (a.highRisk) highRisk++;
    }
    return { application, delegated, secrets, certs, expiring, expired, none, highRisk };
  }, [apps]);

  const permFacet = (data?.facets.permissions ?? []).filter((f) =>
    permSearch.trim() ? f.value.toLowerCase().includes(permSearch.trim().toLowerCase()) : true,
  );
  const stateCounts = new Map((data?.facets.enterpriseAppStates ?? []).map((f) => [f.value, f.count]));
  const signinCounts = new Map((data?.facets.signInActivity ?? []).map((f) => [f.value, f.count]));
  const signinMeta = data?.signin_activity;
  const signinWindow = signinMeta?.window_days ?? 30;
  const failuresMeasured = signinMeta?.failures?.measured ?? false;
  const failuresReason = signinMeta?.failures?.reason ?? "";

  const anyFilter =
    audSel.size || stateSel.size || permTypeSel.size || credSel.size || highRiskOnly || permSel.size || ownerSel.size || signinSel.size || text.trim();

  function clearAll() {
    setAudSel(new Set());
    setStateSel(new Set());
    setPermTypeSel(new Set());
    setCredSel(new Set());
    setHighRiskOnly(false);
    setPermSel(new Set());
    setOwnerSel(new Set());
    setSigninSel(new Set());
    setText("");
  }

  function exportCsv() {
    const rows = [
      ["Name", "AppId", "Audience", "EnterpriseAppState", "ServicePrincipalId", "ServicePrincipalType", "MicrosoftDisableStatus", "Secrets", "Certs", "AppPerms", "DelegatedPerms", "NextExpiryDays", "HighRisk", "Owners", "LastSignIn", "LastFailedSignIn", "SignInStatus"],
      ...filtered.map((a) => [
        a.displayName,
        a.appId,
        a.signInAudience,
        ENTERPRISE_STATE_META[a.enterpriseAppState ?? "unknown"].label,
        a.servicePrincipalId ?? "",
        a.servicePrincipalType ?? "",
        a.disabledByMicrosoftStatus ?? "",
        String(a.secretsCount),
        String(a.certsCount),
        String(a.applicationPermissionsCount),
        String(a.delegatedPermissionsCount),
        a.nextExpiryDays == null ? "" : String(a.nextExpiryDays),
        a.highRisk ? "yes" : "no",
        a.owners.join("; "),
        a.lastSignIn ?? "",
        a.lastFailedSignIn ?? "",
        signinBucket(a),
      ]),
    ];
    const csv = rows.map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "app-registrations.csv";
    link.click();
    URL.revokeObjectURL(url);
    // IU6 — confirm the export (count reflects the active filters).
    setMsg({ text: `Exported ${filtered.length} app registration${filtered.length === 1 ? "" : "s"} to CSV`, ok: true });
  }

  // IU5 — toggle a credential facet from a KPI tile.
  const toggleCred = (c: CredFilter) => { const n = new Set(credSel); n.has(c) ? n.delete(c) : n.add(c); setCredSel(n); };
  const toggleState = (state: EnterpriseAppState) => { const n = new Set(stateSel); n.has(state) ? n.delete(state) : n.add(state); setStateSel(n); };
  const toggleSignin = (b: SignInBucket) => { const n = new Set(signinSel); n.has(b) ? n.delete(b) : n.add(b); setSigninSel(n); };
  // "No recent sign-in" is TWO buckets: no row at all, and a date older than the window.
  // The KPI counts both, so clicking it must select both or the count won't match the grid.
  const dormantActive = DORMANT_BUCKETS.every((b) => signinSel.has(b));
  const toggleDormant = () => {
    const n = new Set(signinSel);
    for (const b of DORMANT_BUCKETS) dormantActive ? n.delete(b) : n.add(b);
    setSigninSel(n);
  };

  const s = data?.summary;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-6 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-gray-500">
              {data?.never_loaded ? (
                <span className="text-amber-600">Never loaded — press Refresh</span>
              ) : data ? (
                <>
                  Last refreshed {agoText(data.age_seconds)}
                  <span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">server cache</span>
                </>
              ) : (
                "—"
              )}
            </span>
            <button
              onClick={exportCsv}
              disabled={!filtered.length}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              ⬇ Export CSV
            </button>
            <a
              href={data && !data.never_loaded && data.apps.length ? api.appRegistrationsWorkbookUrl(connectionId) : undefined}
              aria-disabled={!data || data.never_loaded || !data.apps.length}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium ${
                data && !data.never_loaded && data.apps.length
                  ? "border-green-300 bg-green-50 text-green-700 hover:bg-green-100"
                  : "pointer-events-none border bg-white text-gray-400 opacity-50"
              }`}
              title="Download a multi-sheet Excel workbook: Applications, Credentials, API Permissions, Owners, High Risk, Deactivated and a Permission pivot (all apps)"
            >
              ⬇ Excel (all sheets)
            </a>
            <select
              aria-label="Application registration refresh scope"
              value={refreshMode}
              disabled={refreshing}
              onChange={(e) => setRefreshMode(e.target.value as AppRegRefreshMode)}
              title="Choose the configured safety cap or intentionally enumerate the full tenant"
              className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 disabled:opacity-50"
            >
              <option value="capped">First {data?.configured_limit ?? 500}</option>
              <option value="full">Full tenant</option>
            </select>
            <button
              data-testid="appregs-refresh"
              onClick={() => void doRefresh()}
              disabled={refreshing}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : refreshMode === "full" ? "↻ Refresh full tenant" : "↻ Refresh"}
            </button>
            {refreshing && (
              <button
                data-testid="appregs-cancel"
                onClick={() => void cancelRefresh()}
                disabled={cancelling}
                className="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
              >
                {cancelling ? "Cancelling…" : "Cancel"}
              </button>
            )}
            {progress.length > 0 && (
              <button
                onClick={() => setShowProgress((v) => !v)}
                className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
              >
                {showProgress ? "Hide progress" : `Progress (${progress.length})`}
              </button>
            )}
          </div>
        </div>

        {/* Source provenance */}
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-400">
          <span>Source: {data?.source === "microsoft_graph" ? "Microsoft Graph" : data?.source === "unavailable" ? "unavailable" : "demo dummy data"}</span>
          {data?.note && <span className="text-amber-600">· {data.note}</span>}
          {data?.enumeration && (
            <span>
              · {data.enumeration.fetched}{data.enumeration.graph_total != null ? ` of ${data.enumeration.graph_total}` : ""} fetched
              {` · ${data.enumeration.pages} page${data.enumeration.pages === 1 ? "" : "s"}`}
              {` · ${data.enumeration.duration_seconds.toLocaleString()}s`}
              {data.enumeration.resumed ? " · resumed" : ""}
              {data.enumeration.retries ? ` · ${data.enumeration.retries} retries` : ""}
            </span>
          )}
        </div>

        {/* KPI row */}
        {s && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-10">
            <Kpi label="App registrations" value={s.total} active={!anyFilter} onClick={clearAll} />
            <Kpi label="Deactivated" value={s.deactivated ?? 0} tone={s.deactivated ? "text-red-600" : undefined} active={stateSel.has("deactivated")} onClick={() => toggleState("deactivated")} />
            <Kpi label="With secrets" value={s.withSecrets} active={credSel.has("secrets")} onClick={() => toggleCred("secrets")} />
            <Kpi label="With certs" value={s.withCerts} active={credSel.has("certs")} onClick={() => toggleCred("certs")} />
            <Kpi label="Expiring ≤30d" value={s.expiringSoon} tone={s.expiringSoon ? "text-orange-600" : undefined} active={credSel.has("expiring")} onClick={() => toggleCred("expiring")} />
            <Kpi label="Expired creds" value={s.expired} tone={s.expired ? "text-red-600" : undefined} active={credSel.has("expired")} onClick={() => toggleCred("expired")} />
            <Kpi label="High risk" value={s.highRisk} tone={s.highRisk ? "text-red-600" : undefined} active={highRiskOnly} onClick={() => setHighRiskOnly(!highRiskOnly)} />
            <Kpi label="Ownerless" value={s.ownerless} tone={s.ownerless ? "text-amber-600" : undefined} active={ownerSel.has("(ownerless)")} onClick={() => { const n = new Set(ownerSel); n.has("(ownerless)") ? n.delete("(ownerless)") : n.add("(ownerless)"); setOwnerSel(n); }} />
            <Kpi label={`No sign-in ≥${signinWindow}d`} value={s.noRecentSignIn ?? 0} tone={s.noRecentSignIn ? "text-amber-600" : undefined} active={dormantActive} onClick={toggleDormant} />
            <Kpi label="App / Delegated perms" value={s.applicationPerms + s.delegatedPerms} />
          </div>
        )}
        {/* One honest banner beats 500 "unknown" cells: say WHY the column is empty. */}
        {signinMeta && !signinMeta.measured && (
          <div data-testid="appregs-signin-banner" className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800">
            <b>Sign-in activity not measured.</b> {signinMeta.reason} Until it can be read, an empty
            Last sign-in column means “unknown” — not “never used”.
          </div>
        )}
        {signinMeta && signinMeta.measured && signinMeta.stale && (
          <div data-testid="appregs-signin-stale-banner" className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800">
            <b>Sign-in report is out of date.</b> {signinMeta.stale_reason} Applications it does not
            mention show “not measured” rather than “no sign-in”.
          </div>
        )}
        {/* IU4 — stale-cache nudge once the snapshot is more than a day old. */}
        {data && !data.never_loaded && typeof data.age_seconds === "number" && data.age_seconds > 24 * 3600 && (
          <div className="mt-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-700">
            App-registration snapshot is {agoText(data.age_seconds)} — credentials &amp; owners may have changed.
            <button onClick={() => void doRefresh()} disabled={refreshing} className="rounded border border-amber-300 px-1.5 py-0.5 font-medium hover:bg-amber-100 disabled:opacity-50">Refresh</button>
          </div>
        )}
      </div>

      {/* Live progress log for the (slow, background) refresh */}
      {showProgress && (progress.length > 0 || refreshing) && (
        <div className="mx-6 mt-2 overflow-hidden rounded-lg border border-gray-200 bg-gray-900">
          <div className="flex items-center justify-between border-b border-gray-700 px-3 py-1.5 text-[11px] text-gray-300">
            <span className="flex items-center gap-2">
              {refreshing && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />}
              {refreshing ? "Refresh in progress — runs in the background; you can navigate away" : "Refresh log"}
            </span>
            <span className="tabular-nums text-gray-500">{progress.length} step(s)</span>
          </div>
          <div ref={logRef} className="max-h-56 overflow-auto px-3 py-2 font-mono text-[11px] leading-relaxed">
            {refreshing && liveProgress && (
              <div data-testid="appregs-page-progress" className="mb-2 rounded border border-gray-700 bg-gray-800 px-2 py-1.5 font-sans text-[11px] text-gray-300">
                <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                  <span>
                    {liveProgress.current ?? 0}{liveProgress.total != null ? ` of ${liveProgress.total}` : ""} fetched
                    {liveProgress.page ? ` · page ${liveProgress.page}` : ""}
                  </span>
                  <span>
                    {progressPercent != null ? `${progressPercent.toFixed(1)}%` : "total pending"}
                    {liveProgress.retries ? ` · ${liveProgress.retries} retries` : ""}
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-gray-700">
                  <div className={`h-full rounded-full bg-emerald-400 transition-all ${progressPercent == null ? "w-1/4 animate-pulse" : ""}`} style={progressPercent == null ? undefined : { width: `${Math.max(0, Math.min(100, progressPercent))}%` }} />
                </div>
              </div>
            )}
            {progress.map((p) => (
              <div
                key={p.seq}
                className={
                  p.level === "error"
                    ? "text-red-400"
                    : p.level === "warn"
                    ? "text-amber-300"
                    : p.level === "ok"
                    ? "text-emerald-300"
                    : "text-gray-300"
                }
              >
                <span className="text-gray-600">{new Date(p.ts).toLocaleTimeString()} </span>
                {p.message}
              </div>
            ))}
            {refreshing && <div className="text-gray-500">…</div>}
          </div>
        </div>
      )}

      {msg && (
        <div className={`mx-6 mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>
          {msg.text}
        </div>
      )}

      {/* Body: facet sidebar + grid */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Facet sidebar */}
        <aside className="w-60 shrink-0 overflow-auto border-r bg-white px-3 py-2">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-700">Filters</span>
            {anyFilter ? (
              <button onClick={clearAll} className="text-[11px] text-brand hover:underline">
                Clear
              </button>
            ) : null}
          </div>

          <FacetGroup title="Enterprise app state">
            {ENTERPRISE_STATES.map((state) => (
              <FacetRow
                key={state}
                label={ENTERPRISE_STATE_META[state].label}
                count={stateCounts.get(state) ?? 0}
                active={stateSel.has(state)}
                onClick={() => toggleState(state)}
              />
            ))}
          </FacetGroup>

          <FacetGroup title="Permission type">
            <FacetRow label="Application" count={facetCounts.application} active={permTypeSel.has("Application")} onClick={() => toggle(permTypeSel, "Application", setPermTypeSel)} />
            <FacetRow label="Delegated" count={facetCounts.delegated} active={permTypeSel.has("Delegated")} onClick={() => toggle(permTypeSel, "Delegated", setPermTypeSel)} />
          </FacetGroup>

          <FacetGroup title="Credentials">
            <FacetRow label="Has secrets" count={facetCounts.secrets} active={credSel.has("secrets")} onClick={() => toggle(credSel, "secrets", setCredSel)} />
            <FacetRow label="Has certificates" count={facetCounts.certs} active={credSel.has("certs")} onClick={() => toggle(credSel, "certs", setCredSel)} />
            <FacetRow label="Expiring ≤30d" count={facetCounts.expiring} active={credSel.has("expiring")} onClick={() => toggle(credSel, "expiring", setCredSel)} />
            <FacetRow label="Expired" count={facetCounts.expired} active={credSel.has("expired")} onClick={() => toggle(credSel, "expired", setCredSel)} />
            <FacetRow label="No credentials" count={facetCounts.none} active={credSel.has("none")} onClick={() => toggle(credSel, "none", setCredSel)} />
          </FacetGroup>

          <FacetGroup title="Risk">
            <FacetRow label="High risk only" count={facetCounts.highRisk} active={highRiskOnly} onClick={() => setHighRiskOnly((v) => !v)} />
          </FacetGroup>

          <FacetGroup title="Sign-in audience">
            {(data?.facets.audiences ?? []).map((f) => (
              <FacetRow
                key={f.value}
                label={AUDIENCE_LABEL[f.value] ?? f.value}
                count={f.count}
                active={audSel.has(f.value)}
                onClick={() => toggle(audSel, f.value, setAudSel)}
              />
            ))}
          </FacetGroup>

          <FacetGroup title="Permissions">
            <input
              value={permSearch}
              onChange={(e) => setPermSearch(e.target.value)}
              placeholder="Filter permissions…"
              className="mb-1 w-full rounded border px-2 py-1 text-xs outline-none focus:border-gray-400"
            />
            <div className="max-h-52 space-y-0.5 overflow-auto">
              {permFacet.map((f) => (
                <FacetRow key={f.value} label={f.value} count={f.count} active={permSel.has(f.value)} onClick={() => toggle(permSel, f.value, setPermSel)} />
              ))}
              {!permFacet.length && <div className="px-2 py-1 text-[11px] text-gray-400">No matches.</div>}
            </div>
          </FacetGroup>

          <FacetGroup title="Owners">
            {(data?.facets.owners ?? []).map((f) => (
              <FacetRow key={f.value} label={f.value} count={f.count} active={ownerSel.has(f.value)} onClick={() => toggle(ownerSel, f.value, setOwnerSel)} />
            ))}
          </FacetGroup>

          <FacetGroup title="Last sign-in">
            {SIGNIN_BUCKETS.map((b) => (
              <FacetRow key={b} label={b} count={signinCounts.get(b) ?? 0} active={signinSel.has(b)} onClick={() => toggleSignin(b)} />
            ))}
          </FacetGroup>
        </aside>

        {/* Grid */}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b bg-white px-4 py-2 text-xs">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Search name, app ID, publisher, tag, owner…"
              className="w-72 rounded-lg border px-2.5 py-1.5 outline-none focus:border-gray-400"
            />
            <span className="text-gray-500">
              {filtered.length} of {apps.length} app registration(s)
            </span>
            {data?.truncated && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700" title={`The completed snapshot contains ${data.enumeration?.fetched ?? data.limit ?? 500}${data.graph_total != null ? ` of ${data.graph_total}` : ""} apps. Raise the configured cap or run Full tenant.`}>
                {data.enumeration?.fetched ?? data.limit ?? 500}{data.graph_total != null ? ` of ${data.graph_total}` : ""} (capped)
              </span>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-auto">
            {q.isLoading ? (
              <div className="p-6"><Skeleton rows={8} /></div>
            ) : q.isError ? (
              <div className="m-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(q.error)}</div>
            ) : data?.never_loaded ? (
              <div className="py-16 text-center text-sm text-gray-400">
                Not loaded yet. Press <b>↻ Refresh</b> to pull the current Entra ID app registrations.
              </div>
            ) : data?.connection_failed ? (
              <div className="mx-auto max-w-xl py-16 text-center">
                <div className="text-2xl">🔌</div>
                <div className="mt-2 text-sm font-medium text-gray-700">Couldn’t read app registrations for this connection</div>
                <div className="mx-auto mt-1 max-w-lg text-xs text-gray-500">{data.note || "This connection can’t authenticate to Microsoft Graph."}</div>
                <div className="mx-auto mt-2 max-w-lg text-[11px] text-gray-400">App Registrations need a service-principal connection (client id + secret/cert) granted <b>Directory.Read.All</b> / <b>Application.Read.All</b>. Fix the connection in Settings → Azure Tenants, then Refresh.</div>
              </div>
            ) : !filtered.length ? (
              <div className="py-16 text-center text-sm text-gray-400">No app registrations match the current filters.</div>
            ) : (
              <>
                {/* Sorting reorders the rows the server actually sent. When the listing was
                    capped that is a narrower claim than the header implies, so say so. */}
                <SortScopeNote
                  shown={data?.enumeration?.fetched ?? data?.limit ?? apps.length}
                  total={data?.graph_total ?? 0}
                  sorted="the loaded app registrations"
                />
                {/* IP1 — virtualized header + rows (expandable detail rendered inline; VirtualList
                    measures variable heights). Was a plain <table> mapping every row. */}
                <div className={`sticky top-0 z-10 grid ${GRID_COLS} gap-0 border-b bg-gray-50 px-3 py-2 text-[11px] uppercase tracking-wide text-gray-500`}>
                  <SortHead label="Name" col="name" sort={sort} setSort={setSort} firstDir={1} />
                  <SortHead label="Audience" col="audience" sort={sort} setSort={setSort} firstDir={1} />
                  <SortHead label="State" col="state" sort={sort} setSort={setSort} />
                  <SortHead label="Secrets" col="secrets" sort={sort} setSort={setSort} align="center" />
                  <SortHead label="Certs" col="certs" sort={sort} setSort={setSort} align="center" />
                  <SortHead label="App perms" col="appPerms" sort={sort} setSort={setSort} align="center" />
                  <SortHead label="Delegated" col="delegated" sort={sort} setSort={setSort} align="center" />
                  <SortHead label="Next expiry" col="nextExpiry" sort={sort} setSort={setSort} firstDir={1}
                            title="Days until the next credential expires — soonest first." />
                  <SortHead label="Last sign-in" col="lastSignIn" sort={sort} setSort={setSort} firstDir={1}
                            title={`From Microsoft's per-application sign-in report, which covers the last ${signinWindow} days. It does NOT separate a successful sign-in from a rejected one, so a date here can be a failed attempt. Sorts oldest first.`} />
                  <SortHead label="Last failed sign-in" col="lastFailed" sort={sort} setSort={setSort}
                            title={failuresMeasured
                              ? "The most recent sign-in attempt that did not succeed."
                              : failuresReason} />
                  <SortHead label="Owners" col="owners" sort={sort} setSort={setSort} firstDir={1} />
                  <SortHead label="Risk" col="risk" sort={sort} setSort={setSort} />
                </div>
                <VirtualList
                  items={sorted}
                  estimateSize={48}
                  max="100%"
                  render={(a: AppRegistration) => {
                    const open = expanded === a.id;
                    return (
                      <div className="border-b border-gray-100">
                        <div
                          onClick={() => setExpanded(open ? null : a.id)}
                          className={`grid cursor-pointer ${GRID_COLS} items-center gap-0 px-3 py-2 text-sm hover:bg-gray-50`}
                        >
                          <div className="flex min-w-0 items-center gap-1.5">
                            <span className="text-gray-400">{open ? "▾" : "▸"}</span>
                            <div className="min-w-0">
                              <div className="flex items-center gap-1.5">
                                <span className="truncate font-medium text-gray-900">{a.displayName}</span>
                                <a href={portalUrl(a)} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} title="Open in Azure portal" className="shrink-0 text-gray-400 hover:text-brand">↗</a>
                              </div>
                              <div className="truncate font-mono text-[10px] text-gray-400">{a.appId}</div>
                            </div>
                          </div>
                          <span className="truncate text-xs text-gray-600">{AUDIENCE_LABEL[a.signInAudience] ?? a.signInAudience}</span>
                          <span className="min-w-0 pr-1"><EnterpriseStateBadge state={a.enterpriseAppState} /></span>
                          <span className="text-center tabular-nums">{a.secretsCount || <span className="text-gray-300">0</span>}</span>
                          <span className="text-center tabular-nums">{a.certsCount || <span className="text-gray-300">0</span>}</span>
                          <span className="text-center tabular-nums">{a.applicationPermissionsCount ? <span className="font-medium text-red-600">{a.applicationPermissionsCount}</span> : <span className="text-gray-300">0</span>}</span>
                          <span className="text-center tabular-nums">{a.delegatedPermissionsCount || <span className="text-gray-300">0</span>}</span>
                          <span><ExpiryBadge days={a.nextExpiryDays} /></span>
                          <span className="min-w-0 pr-1"><LastSignInCell a={a} windowDays={signinWindow} /></span>
                          <span className="min-w-0 pr-1"><LastFailedCell a={a} measured={failuresMeasured} reason={failuresReason} /></span>
                          <span className="truncate text-xs text-gray-600">
                            {a.ownerless ? <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">ownerless</span> : a.owners.join(", ")}
                          </span>
                          <span>{a.highRisk ? <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">high</span> : <span className="text-gray-300">—</span>}</span>
                        </div>
                        {open && (
                          <div className="bg-gray-50/60 px-6 py-3">
                            {a.enterpriseAppState === "deactivated" && (
                              <div data-testid="appregs-deactivated-warning" className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                                <b>Enterprise application deactivated.</b> Its credentials and permissions remain listed because they can become effective again if the service principal is re-enabled.
                              </div>
                            )}
                            {a.disabledByMicrosoftStatus && (
                              <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                                <b>Microsoft disable status:</b> {a.disabledByMicrosoftStatus}
                              </div>
                            )}
                            <div className="mb-3">
                              <a href={portalUrl(a)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-brand/30 bg-white px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/5">↗ Open in Azure portal</a>
                              <span className="ml-2 text-[11px] text-gray-500">
                                Enterprise app: {ENTERPRISE_STATE_META[a.enterpriseAppState ?? "unknown"].label}
                                {a.servicePrincipalType ? ` · ${a.servicePrincipalType}` : ""}
                                {a.servicePrincipalId ? ` · ${a.servicePrincipalId}` : ""}
                              </span>
                            </div>
                            <div className="grid gap-4 lg:grid-cols-2">
                              <div>
                                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">Credentials ({a.credentials.length})</div>
                                {a.credentials.length ? (
                                  <ul className="space-y-1">
                                    {a.credentials.map((c, i) => (
                                      <li key={i} className="flex items-center gap-2 text-xs">
                                        <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] uppercase text-gray-600">{c.type === "certificate" ? "cert" : "secret"}</span>
                                        <span className="text-gray-700">{c.displayName || "(unnamed)"}</span>
                                        <ExpiryBadge days={c.daysUntilExpiry} />
                                        <LastUsedBadge lastUsed={c.lastUsed} known={c.lastUsedKnown} days={c.lastUsedDays} />
                                      </li>
                                    ))}
                                  </ul>
                                ) : (
                                  <div className="text-xs text-gray-400">No credentials (public client).</div>
                                )}
                                {a.tags.length > 0 && (
                                  <div className="mt-2 flex flex-wrap gap-1">
                                    {a.tags.map((t) => (<span key={t} className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] text-sky-700">{t}</span>))}
                                  </div>
                                )}
                              </div>
                              <div>
                                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">API permissions ({a.permissions.length})</div>
                                {a.permissions.length ? (
                                  <ul className="space-y-1">
                                    {a.permissions.map((p, i) => (
                                      <li key={i} className="flex items-center gap-2 text-xs">
                                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${p.type === "Application" ? "bg-violet-100 text-violet-700" : "bg-emerald-100 text-emerald-700"}`}>{p.type}</span>
                                        <span className="font-mono text-gray-700">{p.value}</span>
                                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${RISK_CLS[p.risk]}`}>{p.risk}</span>
                                      </li>
                                    ))}
                                  </ul>
                                ) : (
                                  <div className="text-xs text-gray-400">No API permissions.</div>
                                )}
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  }}
                />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
