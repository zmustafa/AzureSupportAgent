import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type {
  AppSettings, EntraAuthSlice, EntraPattern, EntraRiskyUser, EntraSignalsOverview,
  EntraSignInAggregates,
} from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import {
  Bar, cmp, CoverageBanner, EntraEmpty, EntraTimeWindow, SortTh, useEntraSorted,
  useSortState, useSubTabRoute,
} from "./EntraShared";
import { FederationNote } from "./EntraIdentityFabric";
import { InvestigateLink } from "./InvestigateLink";

type Tab = "overview" | "auth-methods" | "legacy" | "failures" | "risky" | "patterns";

/**
 * One shared empty array for every "data has not arrived yet" case.
 *
 * The sort hooks memoise on row identity, and `?? []` hands them a brand new array on every
 * render, which re-sorts the whole grid each time React thinks about it.
 */
const NO_ROWS: never[] = [];

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "auth-methods", label: "Auth methods" },
  { id: "legacy", label: "Legacy auth" },
  { id: "failures", label: "Failures" },
  { id: "risky", label: "Risky users" },
  { id: "patterns", label: "Patterns" },
];

/**
 * A window that was capped is a window that cannot be reasoned about quantitatively.
 * Every chart in this view renders this banner rather than presenting a partial count as
 * a total — the same discipline the Azure Resource Graph truncation bug taught us.
 */
function SampledBanner({ sampled }: { sampled: boolean }) {
  if (!sampled) return null;
  return (
    <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
      <span className="font-semibold">These numbers are a sample.</span> The sign-in window hit
      its row cap, so counts below are lower bounds and proportions are approximate. Shorten the
      lookback window below and re-collect for exact figures over a shorter period.
    </div>
  );
}

function Unavailable({ what, why }: { what: string; why: string }) {
  return (
    <div className="p-6">
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700">
        <div className="font-semibold text-gray-900">{what} is not available</div>
        <div className="mt-1">{why}</div>
      </div>
    </div>
  );
}

/**
 * A headline number.
 *
 * Clickable when something else on this tab explains it: the overview is a summary, and
 * every figure on it is answered in detail by another sub-view. A number you cannot follow
 * is a dead end on a screen whose whole job is to point somewhere.
 */
function Stat({ label, value, tone = "", hint = "", onClick, action }: {
  label: string; value: string | number; tone?: string; hint?: string;
  onClick?: () => void; action?: string;
}) {
  const Cmp = onClick ? "button" : "div";
  return (
    <Cmp onClick={onClick}
         title={[hint, action].filter(Boolean).join(" — ") || undefined}
         className={`rounded-lg border bg-white p-3 text-left ${
           onClick ? "transition hover:border-brand hover:bg-brand/5" : ""}`}>
      <div className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-500">
        {label}
        {hint && <span className="cursor-help text-gray-400">ⓘ</span>}
      </div>
      <div className={`mt-0.5 text-xl font-semibold ${tone || "text-gray-900"}`}>{value}</div>
      {action && <div className="mt-0.5 text-[10px] text-brand">{action}</div>}
    </Cmp>
  );
}

/** A minimal stacked day chart. Deliberately dependency-free — one shape, one message. */
function DayChart({ days }: { days: { day: string; total: number; failure: number }[] }) {
  const max = Math.max(1, ...days.map((d) => d.total));
  if (!days.length) return <div className="text-xs text-gray-500">No daily data in the window.</div>;
  return (
    <div className="flex h-28 items-end gap-1">
      {days.map((d) => {
        const failureH = Math.round((d.failure / max) * 100);
        const successH = Math.round(((d.total - d.failure) / max) * 100);
        return (
          <div key={d.day} className="flex flex-1 flex-col justify-end" title={`${d.day}: ${d.total} sign-ins, ${d.failure} failed`}>
            <div className="bg-red-400" style={{ height: `${failureH}%` }} />
            <div className="bg-sky-400" style={{ height: `${successH}%` }} />
          </div>
        );
      })}
    </div>
  );
}

type AppRow = EntraSignInAggregates["by_app"][number];
type AppKey = "app" | "signins" | "users" | "rate";

// Module level: a comparator redefined every render defeats the sort memo.
function compareApp(a: AppRow, b: AppRow, key: AppKey): number {
  switch (key) {
    case "app": return cmp.text(a.display_name || a.app_id, b.display_name || b.app_id);
    case "signins": return cmp.num(a.total, b.total);
    case "users": return cmp.num(a.users, b.users);
    case "rate": return cmp.num(a.failure_rate, b.failure_rate);
  }
}

const APP_SORT_LABEL: Record<AppKey, string> = {
  app: "name", signins: "sign-ins", users: "users", rate: "failure rate",
};

/**
 * The sign-in window, changeable from the screen that suffers from it.
 *
 * The row cap is the reason this exists: a busy tenant hits 200,000 events long before the
 * window closes, and every number on the tab becomes a lower bound. The only lever against
 * that is a shorter window, and it used to live in Settings — a different screen, reached
 * by abandoning the one that told you to go there.
 *
 * Two facts, deliberately kept apart: the window the NEXT collection will use, and the
 * window the numbers on screen actually cover. Changing a setting does not re-read Graph,
 * so until a collection runs the page would otherwise be captioned with a window it has
 * not collected.
 */
type LookbackMeta = NonNullable<EntraSignalsOverview["lookback"]>;

const LOOKBACK_CHOICES = [1, 3, 7, 14, 30, 60, 90];

function LookbackControl({ lookback, sampled, onRecollect }: {
  lookback?: LookbackMeta;
  sampled: boolean;
  onRecollect?: (domains?: string[]) => void;
}) {
  const configured = lookback?.days ?? 30;
  const covered = lookback?.data_days ?? null;
  const min = lookback?.min ?? 1;
  const max = lookback?.max ?? 90;
  const [days, setDays] = useState(configured);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  // A window chosen on a previous visit is already the configured one; follow it rather
  // than stranding the select on a stale local value.
  useEffect(() => { setDays(configured); setSaved(false); }, [configured]);

  const choices = LOOKBACK_CHOICES.filter((d) => d >= min && d <= max);
  const pending = days !== configured;
  const staleWindow = covered != null && covered !== configured;

  async function apply() {
    setSaving(true);
    setError("");
    try {
      const body: Partial<AppSettings> = { entra_signin_lookback_days: days };
      await api.updateAppSettings(body);
      setSaved(true);
      // Sign-ins are aggregated at collection time, so the window only takes effect on the
      // next read of Graph. Re-collect just the risk domain rather than the whole directory.
      onRecollect?.(["risk"]);
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-white px-3 py-2 text-[12px]">
      <span className="font-medium text-gray-700">Lookback window</span>
      <select
        value={days}
        onChange={(e) => setDays(Number(e.target.value))}
        className="rounded border px-2 py-1 text-[12px]"
        title="A shorter window is the only lever against the row cap. Sign-ins are counted at collection time, so a change takes effect on the next collection."
      >
        {choices.map((d) => (
          <option key={d} value={d}>{d === 1 ? "1 day" : `${d} days`}</option>
        ))}
      </select>
      {onRecollect && (
        <button
          onClick={() => void apply()}
          disabled={saving || (!pending && !sampled && !staleWindow)}
          className="rounded border border-brand bg-brand px-2 py-1 text-[12px] font-medium text-white disabled:opacity-40"
          title={pending
            ? `Save ${days}-day window and re-collect sign-ins`
            : "Re-collect sign-ins over this window"}
        >
          {saving ? "Starting…" : pending ? "Apply and re-collect" : "Re-collect"}
        </button>
      )}
      <span className="text-gray-500">
        {covered == null
          ? "No sign-in data has been collected yet."
          : `These numbers cover ${covered === 1 ? "1 day" : `${covered} days`}.`}
        {staleWindow && !pending && " Re-collect to apply the window above."}
      </span>
      {saved && !error && (
        <span className="text-emerald-700">Saved — collection started; numbers update when it finishes.</span>
      )}
      {error && (
        <span className="text-red-600">
          {/^.*403.*$/.test(error)
            ? "Changing the collection window needs the settings.write permission."
            : error}
        </span>
      )}
    </div>
  );
}

function OverviewTab({ connectionId, onRecollect, onOpenSub }: {
  connectionId: string | null;
  onRecollect?: (domains?: string[]) => void;
  onOpenSub?: (tab: Tab) => void;
}) {
  const lookbackRef = useRef<HTMLDivElement | null>(null);
  const appsRef = useRef<HTMLDivElement | null>(null);
  const q = useQuery({
    queryKey: ["entra-signals-overview", connectionId],
    queryFn: () => api.entraSignalsOverview(connectionId),
  });
  // Before the guards below, not after: see the note in PatternsTab about what a hook
  // hiding behind an early return does to this panel.
  const [appSort, setAppSort] = useSortState<AppKey>(
    // The API returns by_app ordered by total volume; this reproduces it on first render.
    "signals-overview-apps", { key: "signins", dir: -1 });
  // Sort the whole list and slice afterwards, so "top 15" is top 15 of everything rather
  // than a reshuffle of whichever fifteen happened to arrive first.
  const appRows = useEntraSorted(q.data?.signins?.by_app ?? NO_ROWS, appSort, compareApp);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading sign-in health…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.capabilities.signins) {
    return (
      <Unavailable
        what="Sign-in analysis"
        why={
          d.capabilities.licensed_p1 === false
            ? "Sign-in log retention requires Entra ID P1. Authentication method coverage on the next tab works without it."
            : "AuditLog.Read.All has not been granted to this connection. Every other tab in this view still works."
        }
      />
    );
  }
  const s = d.signins;
  const mfaRate = s.total ? s.mfa_challenged / s.total : 0;
  return (
    <div className="space-y-4 p-4">
      <SampledBanner sampled={s.sampled} />
      <div ref={lookbackRef}>
        <LookbackControl lookback={d.lookback}
                         sampled={Boolean(s.sampled)} onRecollect={onRecollect} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {/* Each figure is a summary of a detail view. Sign-ins and Window have no sibling
            sub-view of their own, so they scroll to the thing on THIS page that answers
            them rather than pretending to navigate somewhere. */}
        <Stat label="Sign-ins" value={s.total.toLocaleString()}
              action="See the applications behind them"
              onClick={() => appsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
        <Stat label="Failure rate" value={`${(s.failure_rate * 100).toFixed(1)}%`}
              tone={s.failure_rate > 0.35 ? "text-red-600" : "text-gray-900"}
              action="Break down by error code"
              onClick={() => onOpenSub?.("failures")} />
        <Stat
          label={s.mfa_metric === "ca_enforced" ? "MFA enforced by CA" : "MFA challenged"}
          value={`${(mfaRate * 100).toFixed(0)}%`}
          action="See who has registered a method"
          onClick={() => onOpenSub?.("auth-methods")}
          hint={s.mfa_metric === "ca_enforced"
            ? "Sign-ins where a Conditional Access policy enforced multi-factor authentication. Microsoft Graph v1.0 does not expose a per-sign-in authentication requirement, so this is the narrower claim we can actually substantiate — it excludes MFA required by other means, such as per-user MFA."
            : undefined}
        />
        <Stat label="Legacy sign-ins" value={s.legacy.reduce((a, r) => a + r.total, 0).toLocaleString()}
              tone={s.legacy_success_users ? "text-red-600" : "text-gray-900"}
              action="Break down by protocol"
              onClick={() => onOpenSub?.("legacy")} />
        <Stat label="Window" value={`${s.lookback_days}d`}
              action="Change the window"
              onClick={() => lookbackRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })} />
      </div>

      <div className="rounded-lg border bg-white p-3">
        <div className="mb-2 text-[13px] font-semibold text-gray-800">
          Daily volume <span className="font-normal text-gray-500">— red is failures</span>
        </div>
        <DayChart days={s.by_day} />
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">Client applications</div>
          {Object.entries(s.by_client_app).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="w-52 shrink-0 truncate text-xs text-gray-600" title={k}>{k}</span>
              <Bar value={v} max={s.total} tone="bg-sky-400" />
              <span className="w-16 text-right text-xs text-gray-600">{v.toLocaleString()}</span>
            </div>
          ))}
        </div>
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">
            Conditional Access outcomes
          </div>
          {Object.entries(s.by_ca_result).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="w-52 shrink-0 truncate text-xs text-gray-600">{k}</span>
              <Bar value={v} max={Math.max(...Object.values(s.by_ca_result), 1)} tone="bg-emerald-400" />
              <span className="w-16 text-right text-xs text-gray-600">{v.toLocaleString()}</span>
            </div>
          ))}
        </div>
      </div>

      <div ref={appsRef} className="rounded-lg border bg-white p-3">
        <div className="mb-2 text-[13px] font-semibold text-gray-800">
          Applications by volume{" "}
          {s.by_app.length > 15 && (
            <span className="font-normal text-gray-500">
              — top 15 of {s.by_app.length.toLocaleString()} by {APP_SORT_LABEL[appSort.key]},{" "}
              {appSort.dir === -1 ? "descending" : "ascending"}
            </span>
          )}
        </div>
        <table className="w-full text-[13px]">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <SortTh label="Application" col="app" sort={appSort} setSort={setAppSort} firstDir={1} />
              <SortTh label="Sign-ins" col="signins" sort={appSort} setSort={setAppSort} />
              <SortTh label="Users" col="users" sort={appSort} setSort={setAppSort} />
              <SortTh label="Failure rate" col="rate" sort={appSort} setSort={setAppSort} />
            </tr>
          </thead>
          <tbody>
            {appRows.slice(0, 15).map((a) => (
              <tr key={a.app_id} className="border-t">
                <td className="py-1 pr-2">{a.display_name || a.app_id}</td>
                <td>{a.total.toLocaleString()}</td>
                <td>{a.users}</td>
                <td className={a.failure_rate > 0.3 ? "text-red-600" : ""}>
                  {(a.failure_rate * 100).toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SliceRow({ label, slice }: { label: string; slice: EntraAuthSlice }) {
  const pct = (n: number) => (slice.total ? `${Math.round((n / slice.total) * 100)}%` : "—");
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 text-[13px] font-semibold text-gray-800">
        {label} <span className="font-normal text-gray-500">({slice.total.toLocaleString()} users)</span>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        {([
          ["MFA registered", slice.registered],
          ["MFA capable", slice.capable],
          ["Passwordless capable", slice.passwordless],
          ["Phishing-resistant", slice.phishing_resistant],
          ["SSPR registered", slice.sspr],
          ["No method at all", slice.none],
        ] as [string, number][]).map(([k, v]) => (
          <div key={k}>
            <div className="text-[11px] text-gray-500">{k}</div>
            <div className="flex items-center gap-2">
              <Bar value={v} max={slice.total || 1}
                   tone={k === "No method at all" ? "bg-red-400" : "bg-emerald-400"} />
              <span className="w-12 text-right text-xs text-gray-700">{pct(v)}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AuthMethodsTab({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-auth-methods", connectionId],
    queryFn: () => api.entraAuthMethods(connectionId),
  });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading registration coverage…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.known) {
    return (
      <Unavailable
        what="Authentication method registration"
        why="The registration report requires Entra ID P1 and Reports.Read.All. Without it we cannot tell whether a user can satisfy an MFA challenge, so the simulator treats everyone as capable and says so."
      />
    );
  }
  const pct = (n: number, total: number) => (total ? Math.round((n / total) * 100) : 0);
  const adminGap = d.privileged.total - d.privileged.registered;
  return (
    <div className="space-y-4 p-4">
      {/* Before the numbers, not after: a registration gap that is really "the provider's
          MFA is invisible to Entra" has to be understood before the chart is read. */}
      <FederationNote fabric={d.identity_fabric} context="auth-methods" />
      <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900">
        The administrator row is the number that matters. Tenant-wide coverage of{" "}
        {pct(d.overall.registered, d.overall.total)}% means nothing while{" "}
        {adminGap === 0 ? "no administrator is" : `${adminGap} administrator${adminGap === 1 ? " is" : "s are"}`}{" "}
        unregistered.
      </div>
      {d.unreported > 0 && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {d.unreported.toLocaleString()} enabled user
          {d.unreported === 1 ? "" : "s"} of {d.enabled_total.toLocaleString()} are absent from the
          registration report and are excluded from every figure below — the report lags newly
          created accounts. They are neither counted as covered nor as a gap.
        </div>
      )}
      <SliceRow label="Administrators" slice={d.privileged} />
      <SliceRow label="All enabled users" slice={d.overall} />

      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">Method distribution</div>
          {Object.entries(d.distribution).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="w-56 shrink-0 truncate text-xs text-gray-600" title={k}>{k}</span>
              <Bar value={v} max={Math.max(...Object.values(d.distribution), 1)} tone="bg-indigo-400" />
              <span className="w-12 text-right text-xs text-gray-600">{v}</span>
            </div>
          ))}
        </div>
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">
            Registration gap <span className="font-normal text-gray-500">({d.gap_total} users)</span>
          </div>
          <div className="max-h-72 overflow-auto">
            {d.gap.map((u) => (
              <div key={u.id} className="flex items-center gap-2 border-b py-1 text-[13px] last:border-b-0">
                {u.privileged && (
                  <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
                    privileged
                  </span>
                )}
                <span className="min-w-0 truncate text-gray-800">{u.upn || u.display_name}</span>
                <InvestigateLink principalId={u.id} label={u.upn || u.display_name} />
              </div>
            ))}
            {!d.gap.length && <div className="text-xs text-gray-500">Every enabled user has a method registered.</div>}
          </div>
          {d.gap_total > d.gap.length && (
            <div className="mt-2 border-t pt-2 text-[11px] text-gray-500">
              Showing the first {d.gap.length.toLocaleString()} of {d.gap_total.toLocaleString()},
              privileged accounts first. Export the findings inbox for the full list.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

type LegacyRow = EntraSignInAggregates["legacy"][number];
type LegacyKey = "protocol" | "attempts" | "succeeded" | "users" | "apps" | "last";

function compareLegacy(a: LegacyRow, b: LegacyRow, key: LegacyKey): number {
  switch (key) {
    case "protocol": return cmp.text(a.protocol, b.protocol);
    case "attempts": return cmp.num(a.total, b.total);
    case "succeeded": return cmp.num(a.success, b.success);
    case "users": return cmp.num(a.users, b.users);
    case "apps": return cmp.num(a.apps, b.apps);
    // A protocol that never succeeded carries no last-success stamp; cmp.date sinks it
    // rather than dating it to 1970 and parking it at one end of the column.
    case "last": return cmp.date(a.last_success, b.last_success);
  }
}

function LegacyTab({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-legacy-auth", connectionId],
    queryFn: () => api.entraLegacyAuth(connectionId),
  });
  // Attempts descending is the order the API already returns.
  const [sort, setSort] = useSortState<LegacyKey>("signals-legacy", { key: "attempts", dir: -1 });
  const rows = useEntraSorted(q.data?.protocols ?? NO_ROWS, sort, compareLegacy);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading legacy authentication…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.capabilities.signins) {
    return <Unavailable what="Legacy authentication analysis"
                        why="Sign-in logs require Entra ID P1 and AuditLog.Read.All." />;
  }
  const succeeding = d.protocols.filter((p) => p.success);
  return (
    <div className="space-y-4 p-4">
      <SampledBanner sampled={d.sampled} />
      {d.policy_gap && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <div className="font-semibold">A blocking policy exists and legacy sign-ins still succeed.</div>
          <div className="mt-1">
            {d.blocking_policies.filter((p) => p.is_enforced).map((p) => p.display_name).join(", ")} is
            enforced, yet {d.successful_users.toLocaleString()} user(s) authenticated over a legacy protocol in this
            window. Something is escaping the policy — check its exclusions and the cohorts below.
          </div>
        </div>
      )}
      {!succeeding.length && (
        <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-900">
          No legacy sign-in succeeded in this window.
        </div>
      )}
      <div className="rounded-lg border bg-white p-3">
        <table className="w-full text-[13px]">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <SortTh label="Protocol" col="protocol" sort={sort} setSort={setSort} firstDir={1} />
              <SortTh label="Attempts" col="attempts" sort={sort} setSort={setSort} />
              <SortTh label="Succeeded" col="succeeded" sort={sort} setSort={setSort} />
              <SortTh label="Users" col="users" sort={sort} setSort={setSort} />
              <SortTh label="Apps" col="apps" sort={sort} setSort={setSort} />
              <SortTh label="Last success" col="last" sort={sort} setSort={setSort} />
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.protocol} className="border-t">
                <td className="py-1 pr-2">{p.protocol}</td>
                <td>{p.total.toLocaleString()}</td>
                <td className={p.success ? "font-semibold text-red-600" : "text-gray-500"}>
                  {p.success.toLocaleString()}
                </td>
                <td>{p.users}</td>
                <td>{p.apps}</td>
                <td className="text-xs text-gray-500">{p.last_success ? p.last_success.slice(0, 16).replace("T", " ") : "—"}</td>
              </tr>
            ))}
            {!d.protocols.length && (
              <tr><td colSpan={6} className="py-3 text-center text-xs text-gray-500">
                No legacy protocol traffic at all in this window.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="rounded-lg border bg-white p-3">
        <div className="mb-1 text-[13px] font-semibold text-gray-800">Blocking policies</div>
        {d.blocking_policies.length ? (
          <ul className="list-disc pl-5 text-[13px] text-gray-700">
            {d.blocking_policies.map((p) => (
              <li key={p.id}>
                {p.display_name} —{" "}
                <span className={p.is_enforced ? "text-green-700" : "text-amber-700"}>{p.state}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-[13px] text-amber-700">
            No Conditional Access policy blocks legacy clients. This is the single highest-yield
            control most tenants are missing.
          </div>
        )}
      </div>
    </div>
  );
}

type CodeRow = EntraSignInAggregates["by_failure_code"][number];
type CodeKey = "code" | "meaning" | "count" | "users";
type FailAppKey = "total" | "app" | "failures" | "rate";

function compareCode(a: CodeRow, b: CodeRow, key: CodeKey): number {
  switch (key) {
    // AADSTS codes are numbers wearing string clothes. Sorted as text, 500121 lands
    // between 50053 and 50126, which is nobody's idea of ascending.
    case "code": return cmp.num(Number(a.code), Number(b.code));
    case "meaning": return cmp.text(a.meaning || a.sample, b.meaning || b.sample);
    case "count": return cmp.num(a.count, b.count);
    case "users": return cmp.num(a.users, b.users);
  }
}

function compareFailApp(a: AppRow, b: AppRow, key: FailAppKey): number {
  switch (key) {
    case "total": return cmp.num(a.total, b.total);
    case "app": return cmp.text(a.display_name || a.app_id, b.display_name || b.app_id);
    case "failures": return cmp.num(a.failure, b.failure);
    case "rate": return cmp.num(a.failure_rate, b.failure_rate);
  }
}

const FAIL_APP_SORT_LABEL: Record<FailAppKey, string> = {
  total: "sign-in volume", app: "name", failures: "failures", rate: "failure rate",
};

function FailuresTab({ connectionId, onOpenSub }: {
  connectionId: string | null;
  onOpenSub?: (tab: Tab) => void;
}) {
  const codesRef = useRef<HTMLDivElement | null>(null);
  const appsRef = useRef<HTMLDivElement | null>(null);
  const q = useQuery({
    queryKey: ["entra-failures", connectionId],
    queryFn: () => api.entraFailures(connectionId),
  });
  const [codeSort, setCodeSort] = useSortState<CodeKey>(
    "signals-failure-codes", { key: "count", dir: -1 });
  const codeRows = useEntraSorted(q.data?.codes ?? NO_ROWS, codeSort, compareCode);
  // The API returns these apps ordered by total sign-in volume, a column this grid does not
  // show. Starting there keeps the first render identical to the response; every arrow is
  // neutral until the reader picks one of the columns that IS on screen.
  const [appSort, setAppSort] = useSortState<FailAppKey>(
    "signals-failure-apps", { key: "total", dir: -1 });
  const failApps = useEntraSorted(q.data?.apps ?? NO_ROWS, appSort, compareFailApp);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading failure clustering…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.capabilities.signins) {
    return <Unavailable what="Failure analysis" why="Sign-in logs require Entra ID P1 and AuditLog.Read.All." />;
  }
  return (
    <div className="space-y-4 p-4">
      <SampledBanner sampled={d.sampled} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {/* Failure rate and total sign-ins are the overview's figures repeated here for
            context; the two counts are the tables on this page. Each goes to whichever of
            those actually explains it. */}
        <Stat label="Failure rate" value={`${(d.failure_rate * 100).toFixed(1)}%`}
              tone={d.failure_rate > 0.35 ? "text-red-600" : "text-gray-900"}
              action="See the codes behind it"
              onClick={() => codesRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
        <Stat label="Total sign-ins" value={d.total.toLocaleString()}
              action="Back to sign-in health"
              onClick={() => onOpenSub?.("overview")} />
        <Stat label="Distinct codes" value={d.codes.length}
              action="Jump to the code table"
              onClick={() => codesRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
        <Stat label="Failing apps" value={d.apps.length}
              action="Jump to the application table"
              onClick={() => appsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
      </div>
      <div ref={codesRef} className="rounded-lg border bg-white p-3">
        <div className="mb-2 text-[13px] font-semibold text-gray-800">
          Failure codes <span className="font-normal text-gray-500">— each named in plain English</span>
        </div>
        <table className="w-full text-[13px]">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <SortTh label="Code" col="code" sort={codeSort} setSort={setCodeSort} />
              <SortTh label="Meaning" col="meaning" sort={codeSort} setSort={setCodeSort} firstDir={1} />
              {/* Two right-aligned numeric columns butted together read as one number:
                  "34,700 1153". The border and padding give the eye a boundary. */}
              <SortTh label="Count" col="count" sort={codeSort} setSort={setCodeSort}
                      align="right" className="w-24 pr-4" />
              <SortTh label="Users" col="users" sort={codeSort} setSort={setCodeSort}
                      align="right" className="w-24 border-l pl-3" />
            </tr>
          </thead>
          <tbody>
            {codeRows.map((c) => (
              <tr key={c.code} className="border-t">
                <td className="py-1 pr-2 font-mono text-xs">{c.code}</td>
                <td className="pr-2">{c.meaning || <span className="text-gray-400">{c.sample || "—"}</span>}</td>
                <td className="pr-4 text-right tabular-nums">{c.count.toLocaleString()}</td>
                <td className="border-l pl-3 text-right tabular-nums">{c.users.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div ref={appsRef} className="rounded-lg border bg-white p-3">
        <div className="mb-2 text-[13px] font-semibold text-gray-800">
          Applications with failures{" "}
          {d.apps.length > 20 && (
            <span className="font-normal text-gray-500">
              — top 20 of {d.apps.length.toLocaleString()} by {FAIL_APP_SORT_LABEL[appSort.key]},{" "}
              {appSort.dir === -1 ? "descending" : "ascending"}
            </span>
          )}
        </div>
        <table className="w-full text-[13px]">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <SortTh label="Application" col="app" sort={appSort} setSort={setAppSort}
                      firstDir={1} className="w-64" />
              <SortTh label="Failures" col="failures" sort={appSort} setSort={setAppSort} />
              <SortTh label="Failure rate" col="rate" sort={appSort} setSort={setAppSort}
                      align="right" className="w-20" />
            </tr>
          </thead>
          <tbody>
            {/* Sorted over every loaded app, then capped, so the caption above is true. */}
            {failApps.slice(0, 20).map((a) => (
              <tr key={a.app_id}>
                <td className="py-0.5 pr-2">
                  <span className="block w-64 truncate text-xs text-gray-600">
                    {a.display_name || a.app_id}
                  </span>
                </td>
                <td className="py-0.5 pr-2">
                  <Bar value={a.failure} max={Math.max(...d.apps.map((x) => x.failure), 1)} tone="bg-red-400" />
                </td>
                <td className="py-0.5 text-right text-xs text-gray-600">
                  {(a.failure_rate * 100).toFixed(0)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

type RiskySortKey = "user" | "level" | "state" | "remediate" | "updated";

// Risk level is an ordered scale, not a word. Sorting it alphabetically puts high between
// none and low, which is the one ordering nobody wants.
const LEVEL_RANK: Record<string, number> = { high: 4, medium: 3, low: 2, none: 1, hidden: 0 };
// Same for state: the two that need a human come first when sorting descending.
const STATE_RANK: Record<string, number> = {
  confirmedCompromised: 4, atRisk: 3, remediated: 2, dismissed: 1, confirmedSafe: 0,
};
// Unknown sits between "no" and "yes": it is not evidence of either.
const REMEDIATE_RANK: Record<string, number> = { yes: 2, unknown: 1, no: 0 };

/**
 * Last-updated as epoch ms, or null when Identity Protection never stamped the user.
 *
 * Still here after the sort moved to `cmp.date` because the time-window brush places users
 * on an axis and has to know which ones cannot be placed at all.
 */
function updatedMs(u: EntraRiskyUser): number | null {
  if (!u.last_updated) return null;
  const t = new Date(u.last_updated).getTime();
  return Number.isNaN(t) ? null : t;
}

function remediateState(u: EntraRiskyUser): string {
  if (u.mfa_registered === null) return "unknown";
  return u.can_self_remediate ? "yes" : "no";
}

function riskyCompare(a: EntraRiskyUser, b: EntraRiskyUser, key: RiskySortKey): number {
  switch (key) {
    case "user": return cmp.text(a.upn || a.name, b.upn || b.name);
    case "level": return cmp.rank(LEVEL_RANK, a.level, b.level);
    case "state": return cmp.rank(STATE_RANK, a.state, b.state);
    case "remediate": return cmp.rank(REMEDIATE_RANK, remediateState(a), remediateState(b));
    // Undated users sink in both directions rather than masquerading as 1970.
    case "updated": return cmp.date(a.last_updated, b.last_updated);
  }
}

function RiskyUsersTab({ connectionId }: { connectionId: string | null }) {
  const [level, setLevel] = useState("");
  const [state, setState] = useState("");
  const [search, setSearch] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [sort, setSort] = useSortState<RiskySortKey>("signals-risky", { key: "updated", dir: -1 });
  const term = useDebounced(search, 200).trim().toLowerCase();
  const q = useQuery({
    queryKey: ["entra-risky-users", connectionId, level, state],
    queryFn: () => api.entraRiskyUsers({ level: level || undefined, state: state || undefined }, connectionId),
  });

  // Level and state are server-side, so a new result set arrives underneath the brush. Keeping
  // the old window would hide rows the user just asked for.
  useEffect(() => { setRange(null); }, [connectionId, level, state]);

  const users = q.data?.users ?? NO_ROWS;
  const filtered = useMemo(() => users.filter((u) => {
    if (term && !`${u.upn ?? ""} ${u.name ?? ""}`.toLowerCase().includes(term)) return false;
    if (range) {
      const t = updatedMs(u);
      // A user Identity Protection never stamped cannot be placed on the axis, so a brushed
      // window excludes it rather than pretending it belongs at either end.
      if (t === null || t < range[0] || t > range[1]) return false;
    }
    return true;
  }), [users, term, range]);
  const rows = useEntraSorted(filtered, sort, riskyCompare);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading Identity Protection…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.capabilities.risky_users) {
    return (
      <Unavailable
        what="Identity Protection"
        why="Risky users and risk detections require Entra ID P2 and IdentityRiskyUser.Read.All. Sign-in analysis on the other tabs works without P2."
      />
    );
  }
  const undated = users.filter((u) => updatedMs(u) === null).length;
  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search user or UPN…"
          className="w-56 rounded border px-2 py-1 text-[13px]"
        />
        <select value={level} onChange={(e) => setLevel(e.target.value)}
                className="rounded border px-2 py-1 text-[13px]">
          <option value="">Any level</option>
          <option value="high">High</option><option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
        <select value={state} onChange={(e) => setState(e.target.value)}
                className="rounded border px-2 py-1 text-[13px]">
          <option value="">Any state</option>
          <option value="atRisk">At risk</option>
          <option value="confirmedCompromised">Confirmed compromised</option>
          <option value="remediated">Remediated</option>
          <option value="dismissed">Dismissed</option>
        </select>
        {(term || range) && (
          <button onClick={() => { setSearch(""); setRange(null); }}
                  className="text-[12px] text-brand underline underline-offset-2">
            clear filter
          </button>
        )}
        <span className="ml-auto text-xs text-gray-500">
          {rows.length.toLocaleString()} of {d.total.toLocaleString()} user(s)
        </span>
      </div>

      <EntraTimeWindow
        label="Risk last updated"
        unit="user"
        hotLabel="high risk"
        points={users.map((u) => ({ t: updatedMs(u) ?? NaN, hot: u.level === "high" }))}
        value={range}
        onChange={setRange}
        shownCount={rows.length}
      />
      {range && undated > 0 && (
        <div className="-mt-2 text-[11px] text-gray-500">
          {undated.toLocaleString()} user(s) carry no last-updated stamp and are excluded while a
          window is selected.
        </div>
      )}

      <div className="rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr className="border-b">
              <SortTh label="User" col="user" sort={sort} setSort={setSort} firstDir={1} className="px-3" />
              <SortTh label="Level" col="level" sort={sort} setSort={setSort} />
              <SortTh label="State" col="state" sort={sort} setSort={setSort} />
              <SortTh label="Can self-remediate?" col="remediate" sort={sort} setSort={setSort} />
              <SortTh label="Last updated" col="updated" sort={sort} setSort={setSort} />
            </tr>
          </thead>
          <tbody>
            {rows.map((u: EntraRiskyUser) => (
              <tr key={u.id} className="border-b last:border-b-0">
                <td className="px-3 py-1.5">
                  <div className="flex items-center gap-1.5">
                    {u.privileged && (
                      <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
                        privileged
                      </span>
                    )}
                    <span className="min-w-0 truncate text-gray-900">{u.upn || u.name}</span>
                    <InvestigateLink principalId={u.id} label={u.upn || u.name} />
                  </div>
                </td>
                <td className={u.level === "high" ? "font-semibold text-red-600" : ""}>{u.level}</td>
                <td>{u.state}</td>
                <td className={u.can_self_remediate ? "text-gray-600" : "font-medium text-amber-700"}>
                  {u.mfa_registered === null ? "unknown" : u.can_self_remediate ? "yes" : "no — no MFA method"}
                </td>
                <td className="text-xs text-gray-500">
                  {u.last_updated ? u.last_updated.slice(0, 10) : "—"}
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={5} className="py-4 text-center text-xs text-gray-500">
                {users.length
                  ? "No risky user matches this search or window. Widen the slider or clear the filter."
                  : "No risky users match this filter."}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">Detection types</div>
          {Object.entries(d.detection_counts).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="w-56 shrink-0 truncate text-xs text-gray-600">{k}</span>
              <Bar value={v} max={Math.max(...Object.values(d.detection_counts), 1)} tone="bg-purple-400" />
              <span className="w-10 text-right text-xs text-gray-600">{v}</span>
            </div>
          ))}
          {!Object.keys(d.detection_counts).length && (
            <div className="text-xs text-gray-500">No detections in the window.</div>
          )}
        </div>
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-[13px] font-semibold text-gray-800">Risky workload identities</div>
          {d.workload_identities.length ? d.workload_identities.map((s) => (
            <div key={s.id} className="border-b py-1 text-[13px] last:border-b-0">
              <span className="text-gray-900">{s.name}</span>{" "}
              <span className={s.level === "high" ? "text-red-600" : "text-gray-500"}>({s.level}, {s.state})</span>
            </div>
          )) : (
            <div className="text-xs text-gray-500">
              {d.capabilities.risky_workload_identities
                ? "No workload identity is currently flagged."
                : "Requires Entra Workload Identities Premium."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Render one evidence value from a detection pattern.
 *
 * Evidence is deliberately open-ended — each pattern kind carries whatever proves it — so
 * this has to cope with anything JSON can hold. `String(v)` was fine until the unmanaged
 * -device pattern was aggregated into a single row carrying `top_accounts`, at which point
 * every reader saw "top_accounts: [object Object]". Objects and arrays are summarized by
 * whichever field actually names the thing; nothing is allowed to stringify to noise.
 */
function evidenceText(value: unknown): string {
  const name = (v: unknown): string => {
    if (v === null || v === undefined) return "";
    if (typeof v !== "object") return String(v);
    const o = v as Record<string, unknown>;
    for (const key of ["label", "upn", "display_name", "name", "id", "ip"]) {
      if (typeof o[key] === "string" && o[key]) return o[key] as string;
    }
    return Object.entries(o).map(([k, x]) => `${k}=${String(x)}`).join(" ");
  };
  if (Array.isArray(value)) {
    if (!value.length) return "none";
    const shown = value.slice(0, 5).map(name).filter(Boolean);
    const rest = value.length - shown.length;
    return rest > 0 ? `${shown.join(", ")} +${rest} more` : shown.join(", ");
  }
  if (value === null || value === undefined) return "\u2014";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return name(value);
}

function PatternsTab({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-patterns", connectionId],
    queryFn: () => api.entraPatterns(connectionId),
  });
  // Every hook runs on every render, before any early return. Placing this useMemo after
  // the loading/error guards changed the hook count between renders and crashed the panel
  // with "Rendered more hooks than during the previous render" — but only once the tenant
  // actually had patterns, which is why it survived the demo data.
  const byKind = useMemo(() => {
    const out: Record<string, EntraPattern[]> = {};
    for (const p of q.data?.patterns ?? []) (out[p.kind] ||= []).push(p);
    return out;
  }, [q.data?.patterns]);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading patterns…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.capabilities.signins) {
    return <Unavailable what="Pattern detection" why="Sign-in logs require Entra ID P1 and AuditLog.Read.All." />;
  }
  return (
    <div className="space-y-4 p-4">
      <SampledBanner sampled={d.sampled} />
      <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900">
        These are counting rules, not predictions. Each result states the exact rule that
        produced it and carries the raw counts, so you can verify the claim rather than
        trust it.
      </div>
      {!d.patterns.length && (
        <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-900">
          No pattern crossed its threshold in this window.
        </div>
      )}
      {Object.entries(byKind).map(([kind, rows]) => (
        <div key={kind} className="rounded-lg border bg-white p-3">
          <div className="mb-1 text-[13px] font-semibold text-gray-800">
            {kind.replace(/_/g, " ")} <span className="font-normal text-gray-500">({rows.length})</span>
          </div>
          <div className="mb-2 text-xs italic text-gray-500">{rows[0].rule}</div>
          {rows.map((p) => (
            <div key={p.key} className="border-t py-1.5">
              <div className="text-[13px] text-gray-900">{p.label}</div>
              <div className="mt-0.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-gray-500">
                {Object.entries(p.evidence)
                  // A key with nothing behind it ("upn:" and then blank) reads as a bug.
                  // Where a value is genuinely absent the neighbouring key already says so.
                  .filter(([, v]) => v !== null && v !== undefined && v !== ""
                    && !(Array.isArray(v) && !v.length))
                  .map(([k, v]) => (
                    <span key={k}>{k}: <span className="text-gray-700">{evidenceText(v)}</span></span>
                  ))}
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function EntraSignalsView({ connectionId, onOpenSetup, onRecollect }:
  { connectionId: string | null; onOpenSetup?: () => void;
    onRecollect?: (domains?: string[]) => void }) {
  const [tab, setTab] = useSubTabRoute(TABS.map((t) => t.id), "overview");
  const statusQ = useQuery({
    queryKey: ["entra-status", connectionId],
    queryFn: () => api.entraStatus(connectionId),
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      {statusQ.data && (
        <CoverageBanner meta={statusQ.data.meta} onOpenSetup={onOpenSetup} />
      )}
      <div className="flex shrink-0 gap-1 border-b bg-white px-3 pt-2">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
                  className={`rounded-t px-3 py-1.5 text-[13px] ${
                    tab === t.id ? "border border-b-white bg-white font-medium text-gray-900"
                                 : "text-gray-600 hover:text-gray-900"}`}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "overview" && (
          <OverviewTab connectionId={connectionId} onRecollect={onRecollect} onOpenSub={setTab} />
        )}
        {tab === "auth-methods" && <AuthMethodsTab connectionId={connectionId} />}
        {tab === "legacy" && <LegacyTab connectionId={connectionId} />}
        {tab === "failures" && <FailuresTab connectionId={connectionId} onOpenSub={setTab} />}
        {tab === "risky" && <RiskyUsersTab connectionId={connectionId} />}
        {tab === "patterns" && <PatternsTab connectionId={connectionId} />}
      </div>
    </div>
  );
}
