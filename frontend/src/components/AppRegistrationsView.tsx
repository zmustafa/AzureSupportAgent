import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Fragment } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, streamAppRegistrationsRefresh, type AppRegProgress, type AppRegistration, type AppRegistrationsResponse } from "../api";
import { formatError } from "../utils/format";

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

function Kpi({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

export function AppRegistrationsView({ connectionId = null }: { connectionId?: string | null }) {
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Live progress log for the (slow, background) refresh. Each entry is one streamed step.
  const [progress, setProgress] = useState<AppRegProgress[]>([]);
  const [showProgress, setShowProgress] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Filters
  const [text, setText] = useState("");
  const [audSel, setAudSel] = useState<Set<string>>(new Set());
  const [permTypeSel, setPermTypeSel] = useState<Set<"Application" | "Delegated">>(new Set());
  const [credSel, setCredSel] = useState<Set<CredFilter>>(new Set());
  const [highRiskOnly, setHighRiskOnly] = useState(false);
  const [permSel, setPermSel] = useState<Set<string>>(new Set());
  const [ownerSel, setOwnerSel] = useState<Set<string>>(new Set());
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
  const followStream = useCallback(() => {
    setRefreshing(true);
    setShowProgress(true);
    setProgress([]);
    setMsg(null);
    void streamAppRegistrationsRefresh(
      {
        onProgress: (p) => setProgress((prev) => [...prev, p]),
        onDone: (fresh) => {
          qc.setQueryData(["appRegistrations", connectionId], fresh);
          setRefreshing(false);
          setMsg({ text: `Refreshed — ${fresh.summary?.total ?? 0} app registration(s).`, ok: true });
        },
        onError: (m) => {
          setRefreshing(false);
          setMsg({ text: m, ok: false });
        },
      },
      connectionId,
    ).catch((e) => {
      setRefreshing(false);
      setMsg({ text: formatError(e), ok: false });
    });
  }, [qc, connectionId]);

  function doRefresh() {
    followStream();
  }

  // On mount: if a background refresh is already running (e.g. started on another tab or
  // before navigating away), re-attach to its live progress automatically.
  useEffect(() => {
    let cancelled = false;
    void api
      .appRegistrationsJob(connectionId)
      .then((r) => {
        if (!cancelled && r.job && r.job.status === "running") followStream();
      })
      .catch(() => {
        /* ignore — no job yet */
      });
    return () => {
      cancelled = true;
    };
  }, [followStream]);

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
    const t = text.trim().toLowerCase();
    if (t) {
      const hay = `${a.displayName} ${a.appId} ${a.publisherDomain} ${a.tags.join(" ")} ${a.owners.join(" ")}`.toLowerCase();
      if (!hay.includes(t)) return false;
    }
    return true;
  }

  const filtered = useMemo(() => apps.filter(matches), [apps, audSel, permTypeSel, credSel, highRiskOnly, permSel, ownerSel, text]);

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

  const anyFilter =
    audSel.size || permTypeSel.size || credSel.size || highRiskOnly || permSel.size || ownerSel.size || text.trim();

  function clearAll() {
    setAudSel(new Set());
    setPermTypeSel(new Set());
    setCredSel(new Set());
    setHighRiskOnly(false);
    setPermSel(new Set());
    setOwnerSel(new Set());
    setText("");
  }

  function exportCsv() {
    const rows = [
      ["Name", "AppId", "Audience", "Secrets", "Certs", "AppPerms", "DelegatedPerms", "NextExpiryDays", "HighRisk", "Owners"],
      ...filtered.map((a) => [
        a.displayName,
        a.appId,
        a.signInAudience,
        String(a.secretsCount),
        String(a.certsCount),
        String(a.applicationPermissionsCount),
        String(a.delegatedPermissionsCount),
        a.nextExpiryDays == null ? "" : String(a.nextExpiryDays),
        a.highRisk ? "yes" : "no",
        a.owners.join("; "),
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
  }

  const s = data?.summary;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900">Application Registrations</h1>
            <p className="text-xs text-gray-500">
              Snapshot of Entra ID app registrations — credentials, API permissions and owners.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
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
              title="Download a multi-sheet Excel workbook: Applications, Credentials, API Permissions, Owners, High Risk and a Permission pivot (all apps)"
            >
              ⬇ Excel (all sheets)
            </a>
            <button
              onClick={() => void doRefresh()}
              disabled={refreshing}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : "↻ Refresh"}
            </button>
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
          <span>Source: {data?.source === "microsoft_graph" ? "Microsoft Graph" : "demo dummy data"}</span>
          {data?.note && <span className="text-amber-600">· {data.note}</span>}
        </div>

        {/* KPI row */}
        {s && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
            <Kpi label="App registrations" value={s.total} />
            <Kpi label="With secrets" value={s.withSecrets} />
            <Kpi label="With certs" value={s.withCerts} />
            <Kpi label="Expiring ≤30d" value={s.expiringSoon} tone={s.expiringSoon ? "text-orange-600" : undefined} />
            <Kpi label="Expired creds" value={s.expired} tone={s.expired ? "text-red-600" : undefined} />
            <Kpi label="High risk" value={s.highRisk} tone={s.highRisk ? "text-red-600" : undefined} />
            <Kpi label="Ownerless" value={s.ownerless} tone={s.ownerless ? "text-amber-600" : undefined} />
            <Kpi label="App / Delegated perms" value={s.applicationPerms + s.delegatedPerms} />
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
          </div>

          <div className="min-h-0 flex-1 overflow-auto">
            {q.isLoading ? (
              <div className="py-16 text-center text-sm text-gray-400">Loading app registrations…</div>
            ) : q.isError ? (
              <div className="m-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(q.error)}</div>
            ) : data?.never_loaded ? (
              <div className="py-16 text-center text-sm text-gray-400">
                Not loaded yet. Press <b>↻ Refresh</b> to pull the current Entra ID app registrations.
              </div>
            ) : !filtered.length ? (
              <div className="py-16 text-center text-sm text-gray-400">No app registrations match the current filters.</div>
            ) : (
              <table className="w-full border-collapse text-sm">
                <thead className="sticky top-0 z-10 bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-3 py-2 font-semibold">Name</th>
                    <th className="px-3 py-2 font-semibold">Audience</th>
                    <th className="px-3 py-2 text-center font-semibold">Secrets</th>
                    <th className="px-3 py-2 text-center font-semibold">Certs</th>
                    <th className="px-3 py-2 text-center font-semibold">App perms</th>
                    <th className="px-3 py-2 text-center font-semibold">Delegated</th>
                    <th className="px-3 py-2 font-semibold">Next expiry</th>
                    <th className="px-3 py-2 font-semibold">Owners</th>
                    <th className="px-3 py-2 font-semibold">Risk</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((a) => {
                    const open = expanded === a.id;
                    return (
                      <Fragment key={a.id}>
                        <tr
                          onClick={() => setExpanded(open ? null : a.id)}
                          className="cursor-pointer border-b border-gray-100 hover:bg-gray-50"
                        >
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-1.5">
                              <span className="text-gray-400">{open ? "▾" : "▸"}</span>
                              <div className="min-w-0">
                                <div className="flex items-center gap-1.5">
                                  <span className="truncate font-medium text-gray-900">{a.displayName}</span>
                                  <a
                                    href={portalUrl(a)}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    title="Open in Azure portal"
                                    className="shrink-0 text-gray-400 hover:text-brand"
                                  >
                                    ↗
                                  </a>
                                </div>
                                <div className="truncate font-mono text-[10px] text-gray-400">{a.appId}</div>
                              </div>
                            </div>
                          </td>
                          <td className="px-3 py-2 text-xs text-gray-600">{AUDIENCE_LABEL[a.signInAudience] ?? a.signInAudience}</td>
                          <td className="px-3 py-2 text-center tabular-nums">{a.secretsCount || <span className="text-gray-300">0</span>}</td>
                          <td className="px-3 py-2 text-center tabular-nums">{a.certsCount || <span className="text-gray-300">0</span>}</td>
                          <td className="px-3 py-2 text-center tabular-nums">{a.applicationPermissionsCount ? <span className="font-medium text-red-600">{a.applicationPermissionsCount}</span> : <span className="text-gray-300">0</span>}</td>
                          <td className="px-3 py-2 text-center tabular-nums">{a.delegatedPermissionsCount || <span className="text-gray-300">0</span>}</td>
                          <td className="px-3 py-2"><ExpiryBadge days={a.nextExpiryDays} /></td>
                          <td className="px-3 py-2 text-xs text-gray-600">
                            {a.ownerless ? <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">ownerless</span> : a.owners.join(", ")}
                          </td>
                          <td className="px-3 py-2">
                            {a.highRisk ? (
                              <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">high</span>
                            ) : (
                              <span className="text-gray-300">—</span>
                            )}
                          </td>
                        </tr>
                        {open && (
                          <tr className="border-b border-gray-100 bg-gray-50/60">
                            <td colSpan={9} className="px-6 py-3">
                              <div className="mb-3">
                                <a
                                  href={portalUrl(a)}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="inline-flex items-center gap-1.5 rounded-lg border border-brand/30 bg-white px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/5"
                                >
                                  ↗ Open in Azure portal
                                </a>
                              </div>
                              <div className="grid gap-4 lg:grid-cols-2">
                                <div>
                                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                    Credentials ({a.credentials.length})
                                  </div>
                                  {a.credentials.length ? (
                                    <ul className="space-y-1">
                                      {a.credentials.map((c, i) => (
                                        <li key={i} className="flex items-center gap-2 text-xs">
                                          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] uppercase text-gray-600">{c.type === "certificate" ? "cert" : "secret"}</span>
                                          <span className="text-gray-700">{c.displayName || "(unnamed)"}</span>
                                          <ExpiryBadge days={c.daysUntilExpiry} />
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <div className="text-xs text-gray-400">No credentials (public client).</div>
                                  )}
                                  {a.tags.length > 0 && (
                                    <div className="mt-2 flex flex-wrap gap-1">
                                      {a.tags.map((t) => (
                                        <span key={t} className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] text-sky-700">{t}</span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                                <div>
                                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                    API permissions ({a.permissions.length})
                                  </div>
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
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
