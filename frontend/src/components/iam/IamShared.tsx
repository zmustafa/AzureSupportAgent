/** Shared primitives for the IAM (access review) screen.
 *
 *  Everything here is used by two or more tabs: the connection context, the per-scope refresh
 *  hook, the status/freshness badges, and the scope-freshness table. Tab-specific rendering
 *  lives in its own `Iam*.tsx` file.
 */
import { createContext, useContext, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { streamIamRefresh, type IamOverview, type IamProgress, type IamRow, type IamRunTiming, type IamScopeFreshness } from "../../api";

// Active connection/tenant scope for the whole IAM review. "" => default connection.
// Shared via context so every tab + the refresh stream re-scope together without prop drilling.
export const IamConnectionContext = createContext<string>("");
export const useIamConnectionId = () => useContext(IamConnectionContext) || null;
export const IamConnectionChangeContext = createContext<(connectionId: string) => void>(() => undefined);
export const useIamConnectionChange = () => useContext(IamConnectionChangeContext);

// RP6 — access-grid server page size (the grid pages through the full result set as you scroll).
export const IAM_PAGE = 200;

// Every react-query key this screen owns, so a refresh can invalidate them in one place.
export const IAM_QUERY_KEYS = ["overview", "scopes", "access", "pivots", "roles", "diagnostics", "runs"] as const;

/** Scope or workload the access grid / insights are filtered to. `null` means the whole tenant. */
export type AccessFilter = {
  type: "scope" | "workload";
  label: string;
  scope_id?: string;
  subscription_ids?: string;
  workload_id?: string;
};

// ---- helpers --------------------------------------------------------------------
export function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** "scanned 12m ago" for the IAM header — the same freshness affordance /entra carries.
 *
 * Two things this deliberately does NOT do, because either would overstate how current the
 * picture is:
 *
 * 1. The overview's `generated_at` is `max()` across every scope plus the directory
 *    (compose._latest_generated). On a 45-scope tenant, one scope refreshing a minute ago
 *    would render "scanned just now" while the other 44 are days old. So the headline is the
 *    newest, but anything lagging past the TTL is named next to it rather than hidden behind
 *    the reassuring number.
 * 2. Collected is not verified. A delta refresh that skips a scope records `verified_at` and
 *    leaves `generated_at` alone (compose._scope_freshness), precisely so "4 days old,
 *    verified 2 minutes ago" stays tellable from "collected 2 minutes ago". This reports the
 *    collection.
 */
export function IamFreshness({ overview }: { overview?: IamOverview }) {
  // No data yet (still loading, or the query failed) is NOT the same as never scanned. The
  // header renders before the overview resolves, so asserting "never scanned" here would
  // flash a false claim on every visit to a tenant that has been scanned for months.
  if (!overview) return null;
  if (overview.never_loaded) {
    return <span className="text-xs text-gray-500">never scanned</span>;
  }
  const ages = (overview.scopes ?? [])
    .map((s) => s.age_seconds)
    .filter((a): a is number => a != null);
  if (overview.directory?.age_seconds != null) ages.push(overview.directory.age_seconds);
  if (!ages.length) return <span className="text-xs text-gray-500">never scanned</span>;

  const newest = Math.min(...ages);
  const oldest = Math.max(...ages);
  const ttl = overview.ttl_s || 0;
  const behind = ttl > 0 ? ages.filter((a) => a > ttl).length : 0;
  const tone = oldest > 86400 ? "text-red-600" : behind ? "text-amber-600" : "text-gray-500";
  // Only worth naming when the scopes genuinely DISAGREE. If every scope is equally old the
  // headline already tells the whole truth, and "46 of 46 older" is noise that invites the
  // question "older than what?". The split case is the one the headline would misrepresent.
  const split = behind > 0 && behind < ages.length;

  return (
    <span
      className={`text-xs ${tone}`}
      title={
        `Newest collection ${agoText(newest)}; oldest ${agoText(oldest)}.` +
        (behind ? ` ${behind} of ${ages.length} past the ${Math.round(ttl / 60)}m refresh window.` : "") +
        (overview.demo ? " Demo dataset." : "")
      }
    >
      {overview.demo ? "demo data · " : ""}
      scanned {agoText(newest)}
      {split && ` · ${behind} of ${ages.length} scopes stale`}
    </span>
  );
}

const STATUS_CLS: Record<string, string> = {
  Succeeded: "bg-green-100 text-green-700",
  SucceededWithWarnings: "bg-amber-100 text-amber-700",
  PartiallyCollected: "bg-amber-100 text-amber-700",
  Skipped: "bg-gray-100 text-gray-600",
  Unauthorized: "bg-orange-100 text-orange-700",
  Throttled: "bg-orange-100 text-orange-700",
  Failed: "bg-red-100 text-red-700",
};

export function StatusPill({ status }: { status: string }) {
  const cls = STATUS_CLS[status] ?? "bg-sky-100 text-sky-700";
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>{status || "—"}</span>;
}

export function StaleBadge({ stale, age }: { stale?: boolean; age: number | null }) {
  if (age == null) return <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">never</span>;
  const cls = stale ? "bg-amber-100 text-amber-700" : "bg-green-100 text-green-700";
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>updated {agoText(age)}</span>;
}

export function PrivBadge({ row }: { row: IamRow }) {
  if (row.roleIsPrivileged) return <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700">privileged</span>;
  if (row.roleHasDataActions) return <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">data</span>;
  return null;
}

/** Deny marker. Deny assignments are evaluated BEFORE role assignments and cannot be overridden
 *  — not even by Owner — so they must be visually unmistakable in a grid of grants. */
export function EffectChip({ row }: { row: IamRow }) {
  if (row.effect !== "Deny") return null;
  return (
    <span
      className="rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white"
      title="Deny assignment — evaluated before role assignments and cannot be overridden, even by Owner"
    >
      deny
    </span>
  );
}

export const PATH_LABEL: Record<string, string> = {
  Direct: "Direct",
  GroupTransitive: "via group",
  Owner: "owner",
};

// Friendly Scope-column rendering: classify by the row's scopeType and prefix the name —
// "Subscription: <name>", "RG: <name>", "MG: <name>" — with the matching Azure scope icon.
export function scopeCell(r: IamRow): { icon: "mg" | "subscription" | "resource_group" | "resource" | "tenant" | null; label: string } {
  const str = (k: string) => (typeof r[k] === "string" ? (r[k] as string).trim() : "");
  switch (str("scopeType")) {
    case "subscription":
      return { icon: "subscription", label: `Subscription: ${str("subscriptionName") || str("subscriptionId") || "—"}` };
    case "resourceGroup":
      return { icon: "resource_group", label: `RG: ${str("resourceGroup") || "—"}` };
    case "managementGroup":
      return { icon: "mg", label: `MG: ${str("managementGroupName") || str("scopeDisplayName") || str("managementGroupId") || "—"}` };
    case "resource":
      return { icon: "resource", label: str("resourceName") || str("scopeDisplayName") || str("scope") || "Resource" };
    case "tenantRoot":
      return { icon: "tenant", label: str("scopeDisplayName") || "Tenant Root Group" };
    case "directory":
      return { icon: null, label: "Directory" };
    default:
      return { icon: null, label: str("scopeDisplayName") || str("subscriptionName") || str("scope") || "directory" };
  }
}

export function KpiTile({ label, value, tone }: { label: string; value: number | null | undefined; tone?: "red" | "amber" | "sky" }) {
  const toneCls = tone === "red" ? "text-red-600" : tone === "amber" ? "text-amber-700" : tone === "sky" ? "text-sky-700" : "text-gray-900";
  // A missing figure renders as "—", never as 0. A hard zero on a tenant that was never scanned
  // is the most reassuring possible way to say "we did not look", and it is the one rendering
  // this product must never produce.
  const missing = value == null;
  return (
    <div className="rounded-lg border bg-white px-2.5 py-1.5">
      <div
        className={`text-lg font-semibold leading-tight ${missing ? "text-gray-400" : toneCls}`}
        title={missing ? "Not measured — no value was collected for this." : undefined}
      >
        {missing ? "—" : value.toLocaleString()}
      </div>
      <div className="truncate text-[10px] uppercase leading-tight tracking-wide text-gray-500" title={label}>{label}</div>
    </div>
  );
}

// ---- per-scope refresh hook -----------------------------------------------------
export function useIamRefresh() {
  const qc = useQueryClient();
  const connectionId = useIamConnectionId();
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [log, setLog] = useState<IamProgress[]>([]);
  const [activeLabel, setActiveLabel] = useState<string>("");
  // The clock, updated by BOTH progress lines and the server's ticks. Collecting one large
  // subscription runs for minutes without emitting a message, and a frozen "0:04" through that
  // silence is indistinguishable from a hung job.
  const [timing, setTiming] = useState<IamRunTiming | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const invalidate = () => {
    for (const k of IAM_QUERY_KEYS) {
      qc.invalidateQueries({ queryKey: ["iam", k] });
    }
  };

  async function run(params: { scope?: string; mode: string; display_name?: string }, key: string, label: string) {
    if (refreshing.has(key)) return;
    setRefreshing((s) => new Set(s).add(key));
    setActiveLabel(label);
    setLog([]);
    setTiming(null);
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const keepTiming = (d: Partial<IamRunTiming>) => {
      if (typeof d.elapsed_seconds === "number") setTiming(d as IamRunTiming);
    };
    await streamIamRefresh(
      { ...params, connection_id: connectionId ?? undefined },
      {
        onProgress: (d) => {
          setLog((l) => [...l, d]);
          keepTiming(d);
        },
        onTick: keepTiming,
        onDone: (d) => {
          keepTiming(d);
        },
        onError: (msg) => setLog((l) => [...l, { seq: l.length, ts: "", level: "error", message: msg }]),
      },
      ctrl.signal,
    );
    // ONCE. This used to fire here AND from `onDone`, so a finished refresh invalidated seven
    // query keys twice — up to fourteen requests dispatched in the same tick, every one of them
    // landing on caches the run's final write had just discarded, at the exact moment the user
    // thinks the work is over.
    invalidate();
    setRefreshing((s) => {
      const n = new Set(s);
      n.delete(key);
      return n;
    });
  }

  return {
    refreshing,
    log,
    activeLabel,
    timing,
    refreshScope: (scope: string, label: string) => run({ scope, mode: "scope", display_name: label }, scope, label),
    refreshDirectory: () => run({ mode: "directory" }, "directory", "Directory"),
    refreshAll: () => run({ mode: "all" }, "__all__", "All scopes"),
    // Shares the __all__ key with refreshAll on purpose: both write across every scope slice,
    // so the button must be disabled while either is running.
    refreshChanged: () => run({ mode: "delta" }, "__all__", "Changed scopes"),
    isBusy: refreshing.size > 0,
  };
}

export type IamRefreshCtl = ReturnType<typeof useIamRefresh>;

/** The live refresh console: what is running, how long it has been going, how much is left.
 *
 * The estimate is only ever the server's, and the server only gives one when it has this
 * tenant's own measured runs to base it on. When it does not, this shows an em dash and says
 * why — never a placeholder. A bar that claims "8 seconds remaining" for four minutes teaches
 * people the number is decorative, and after that no progress indicator here is believed.
 */
export function RefreshConsole({ ctl, lines = 8 }: { ctl: IamRefreshCtl; lines?: number }) {
  if (ctl.log.length === 0 && !ctl.timing) return null;
  const t = ctl.timing;
  const running = ctl.isBusy;
  // Only draw a bar when there is a real total to draw it against. Progress against an
  // invented denominator is the same lie in a different shape.
  const pct =
    t && t.typical_seconds
      ? Math.min(100, Math.round((t.elapsed_seconds / t.typical_seconds) * 100))
      : null;
  // "Overdue" is a statement about a job still running. A FINISHED job has no time remaining
  // and is not late for anything — saying it is "taking longer than usual" after it completed
  // is simply wrong, and it was the first thing this console got wrong on a real refresh.
  const overdue = running && !!t && t.eta_seconds === null && !!t.typical_seconds;

  return (
    <div className="mb-4 rounded-lg border bg-gray-900 p-2 font-mono text-[11px] text-gray-100">
      <div className="mb-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-gray-400">
          {running ? `Refreshing ${ctl.activeLabel}…` : `${ctl.activeLabel} — finished`}
        </span>
        {t && (
          <>
            <span className="text-gray-300">
              {running ? "elapsed" : "took"} <b className="tabular-nums text-white">{t.elapsed_label}</b>
            </span>
            {running && (
              <span className={overdue ? "text-amber-300" : "text-gray-300"}>
                {overdue ? (
                  "taking longer than usual"
                ) : (
                  <>about <b className="tabular-nums text-white">{t.eta_label}</b> left</>
                )}
              </span>
            )}
            {running && (
              <span className="text-gray-500" title={t.eta_basis}>
                ({t.eta_basis})
              </span>
            )}
          </>
        )}
      </div>
      {running && pct !== null && (
        <div className="mb-1.5 h-1 w-full overflow-hidden rounded bg-gray-700">
          <div
            className={`h-full transition-[width] duration-500 ${overdue ? "bg-amber-400" : "bg-sky-400"}`}
            style={{ width: `${overdue ? 100 : pct}%` }}
          />
        </div>
      )}
      {ctl.log.slice(-lines).map((l, i) => (
        <div
          key={i}
          className={
            l.level === "error"
              ? "text-red-400"
              : l.level === "ok"
                ? "text-green-400"
                : l.level === "warning"
                  ? "text-amber-300"
                  : "text-gray-200"
          }
        >
          {l.elapsed_label && <span className="mr-2 text-gray-500 tabular-nums">{l.elapsed_label}</span>}
          {l.message}
        </div>
      ))}
    </div>
  );
}

// ---- scope freshness table ------------------------------------------------------
export function ScopeTable({
  scopes,
  refresh,
  refreshing,
}: {
  scopes: IamScopeFreshness[];
  refresh: (scope: string, label: string) => void;
  refreshing: Set<string>;
}) {
  if (scopes.length === 0) return <div className="px-4 py-3 text-sm text-gray-500">No scopes scanned yet.</div>;
  return (
    <table className="w-full border-collapse text-sm">
      <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
        <tr>
          <th className="px-3 py-2">Scope</th>
          <th className="px-3 py-2">Type</th>
          <th className="px-3 py-2">Status</th>
          <th className="px-3 py-2">Grants</th>
          <th className="px-3 py-2">Freshness</th>
          <th className="px-3 py-2">Source</th>
          <th className="px-3 py-2"></th>
        </tr>
      </thead>
      <tbody>
        {scopes.map((s) => (
          <tr key={s.scope} className="border-t hover:bg-gray-50">
            <td className="px-3 py-1.5 font-medium text-gray-800">
              {s.displayName}
              {s.demo && <span className="ml-1 rounded bg-violet-100 px-1 text-[10px] text-violet-700">demo</span>}
            </td>
            <td className="px-3 py-1.5 text-gray-500">{s.scopeType}</td>
            <td className="px-3 py-1.5">
              <StatusPill status={s.status} />
              {s.collectors_attention > 0 && <span className="ml-1 text-[11px] text-amber-700">⚠ {s.collectors_attention}</span>}
            </td>
            <td className="px-3 py-1.5 text-gray-600">{s.row_count}</td>
            <td className="px-3 py-1.5">
              <StaleBadge stale={s.stale} age={s.age_seconds} />
              {/* Two separate facts. The age is when the rows were COLLECTED; "verified" means a
                  delta pass confirmed nothing changed since. Collapsing them into one number
                  would let day-old data read as freshly collected. */}
              {s.verified_unchanged && (
                <span
                  className="ml-1 rounded border border-emerald-300 bg-emerald-50 px-1 text-[10px] text-emerald-700"
                  title={`No authorization activity since collection. Verified ${agoText(s.verified_age_seconds ?? null)}.`}
                >
                  ✓ unchanged
                </span>
              )}
            </td>
            <td className="px-3 py-1.5">
              {s.source && (
                <span
                  className="rounded bg-gray-100 px-1 text-[10px] uppercase text-gray-500"
                  title={
                    s.source === "arg"
                      ? "Collected by the tenant-wide Resource Graph sweep."
                      : "Collected by per-scope ARM calls."
                  }
                >
                  {s.source}
                </span>
              )}
            </td>
            <td className="px-3 py-1.5">
              <button
                onClick={() => refresh(s.scope, s.displayName)}
                disabled={refreshing.has(s.scope)}
                className="rounded border px-2 py-0.5 text-xs text-brand hover:bg-gray-50 disabled:opacity-50"
              >
                {refreshing.has(s.scope) ? "Refreshing…" : "↻ Refresh"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
