import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, streamEntraRefresh, type EntraPillar, type EntraPosture, type EntraProgress } from "../api";
import { formatError } from "../utils/format";
import { CONNECTION_KEY, usePersistedState } from "../utils/persistedState";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { ENTRA_NAV, type EntraTab } from "./navConfig";
import { EntraCaView } from "./entra/EntraCaView";
import { EntraAppsView } from "./entra/EntraAppsView";
import { EntraGovernanceView } from "./entra/EntraGovernanceView";
import { EntraGraphView } from "./entra/EntraGraphView";
import { EntraPrivilegedView } from "./entra/EntraPrivilegedView";
import { EntraScannersView } from "./entra/EntraScannersView";
import { EntraInvestigateView } from "./entra/EntraInvestigateView";
import { EntraSignalsView } from "./entra/EntraSignalsView";
import { EntraSetupView } from "./entra/EntraSetupView";
import { Bar, CoverageBanner, EntraEmpty, FreshnessBadge, ScoreRing, SevBadge, StateChip } from "./entra/EntraShared";
import { FederationNote } from "./entra/EntraIdentityFabric";
import { useExportDownload } from "./ExportProgress";

/**
 * Entra ID Support Agent.
 *
 * Answers "who can do what in this tenant, what is exposed, and what breaks if I change it"
 * — as opposed to the Azure estate tooling, which answers "what is wrong with my resources".
 *
 * Reads are cache-only: visiting a screen never triggers a tenant scan. Refresh is the only
 * path that talks to Microsoft Graph, and it runs as a detached background job with SSE
 * progress so a browser reload re-attaches instead of losing the run.
 */
/**
 * A horizontally scrollable tab strip that admits when it is hiding something.
 *
 * Nine Entra tabs do not fit beside the freshness badge and connection picker at 1440px.
 * Plain `overflow-x-auto` solved the layout and created a worse problem: four tabs sat
 * off-screen with no edge cue, so the product looked like it had five screens. The
 * gradients appear only when there is genuinely more in that direction.
 *
 * Three things here are load-bearing against a render loop that made the whole panel
 * flicker, most visibly on the graph screen:
 *   * `setEdges` must bail when nothing changed. A fresh object on every ResizeObserver
 *     tick re-rendered the panel, which resized the strip, which fired the observer again.
 *   * the observer effect must not depend on `children` — that is a new array every render,
 *     so the observer was torn down and rebuilt in a loop.
 *   * bringing the active tab into view uses `scrollLeft` arithmetic, not `scrollIntoView`,
 *     which also scrolls every scrollable ancestor and was flicking the outer horizontal
 *     scrollbar in and out of existence.
 */
function TabScroller({ activeId, children }: { activeId: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 4;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 4;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [measure]);

  useEffect(() => {
    const el = ref.current;
    const active = el?.querySelector<HTMLElement>('[data-active="true"]');
    if (!el || !active) return;
    const start = active.offsetLeft;
    const end = start + active.offsetWidth;
    if (start < el.scrollLeft) el.scrollLeft = start;
    else if (end > el.scrollLeft + el.clientWidth) el.scrollLeft = end - el.clientWidth;
  }, [activeId]);

  return (
    <div className="relative min-w-0 flex-1">
      <div ref={ref} onScroll={measure}
           className="flex items-center gap-1 overflow-x-auto">
        {children}
      </div>
      {edges.left && (
        <div className="pointer-events-none absolute inset-y-0 left-0 w-6 bg-gradient-to-r from-white to-transparent" />
      )}
      {edges.right && (
        <div className="pointer-events-none absolute inset-y-0 right-0 flex w-8 items-center justify-end bg-gradient-to-l from-white via-white to-transparent">
          <span className="text-xs text-gray-400">{"\u203a"}</span>
        </div>
      )}
    </div>
  );
}

export function EntraPanel({ tab = "posture" }: { tab?: EntraTab }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [connectionId, setConnectionId] = usePersistedState(CONNECTION_KEY, "");
  const cid = connectionId || null;

  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState<EntraProgress[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const setTab = (v: EntraTab) => navigate(v === "posture" ? "/entra" : `/entra/${v}`);

  const statusQ = useQuery({
    queryKey: ["entra-status", cid],
    queryFn: () => api.entraStatus(cid),
    refetchInterval: refreshing ? 5000 : false,
  });

  const invalidate = useCallback(() => {
    for (const key of [
      "entra-status", "entra-posture", "entra-findings", "entra-setup", "entra-diagnostics",
      "entra-ca-coverage", "entra-ca-policies", "entra-ca-conflicts", "entra-ca-breakglass",
      "entra-priv-overview", "entra-priv-assignments", "entra-priv-pim", "entra-priv-activity",
      "entra-priv-crossplane", "entra-apps", "entra-apps-consent", "entra-app360",
      "entra-simulations",
      "entra-signals-overview", "entra-auth-methods", "entra-legacy-auth", "entra-failures",
      "entra-risky-users", "entra-patterns",
      "entra-gov-overview", "entra-gov-coverage", "entra-gov-reviews", "entra-gov-entitlement",
      "entra-gov-lifecycle",
      "entra-graph", "entra-graph-targets", "entra-graph-escalations",
      "entra-scanners", "entra-inbox",
    ]) {
      void qc.invalidateQueries({ queryKey: [key] });
    }
  }, [qc]);

  const follow = useCallback(() => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setRefreshing(true);
    void streamEntraRefresh(
      {
        onProgress: (p) => setProgress((prev) => [...prev.slice(-200), p]),
        onDone: () => {
          setRefreshing(false);
          invalidate();
        },
        onError: (msg) => {
          setProgress((prev) => [...prev, { seq: -1, ts: "", level: "error", message: msg }]);
          setRefreshing(false);
          invalidate();
        },
      },
      cid,
      ac.signal,
    );
  }, [cid, invalidate]);

  // Re-attach to an in-flight job on mount / connection switch, so a page reload during a
  // long collection picks the stream back up instead of looking like nothing is happening.
  useEffect(() => {
    if (statusQ.data?.refreshing && !refreshing) {
      setProgress([]);
      follow();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusQ.data?.refreshing]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // `domains` scopes the collection. A tab that only needs its own data re-read (the
  // sign-in window is the case that forced this) should not make the reader wait for the
  // whole directory, and the progress strip below works the same either way.
  const startRefresh = async (domains?: string[]) => {
    setProgress([]);
    // Guarded because this is wired to `onClick` in two places, and a click handler is
    // handed a DOM event as its first argument. Without this, pressing Refresh would post
    // a serialised event object as the domain list.
    const wanted = Array.isArray(domains) && domains.length ? domains : undefined;
    try {
      await api.entraRefresh(cid, wanted);
      follow();
    } catch (err) {
      setProgress([{ seq: -1, ts: "", level: "error", message: formatError(err) }]);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      {/* Title row. Every other screen in the product names itself at the top; this one
          opened straight onto a tab strip, so a shared link landed the reader in a bare
          row of tabs with nothing saying which product surface they were looking at.
          The connection picker and freshness badge live here rather than beside the tabs
          because nine tab labels and two controls do not fit on one 1440px line. */}
      <div className="border-b bg-white px-4 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-xl">🛡️</span>
            <h1 className="text-lg font-bold text-gray-800">Entra ID</h1>
            <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[11px] font-medium text-brand-dark">
              Identity posture
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <FreshnessBadge meta={statusQ.data?.meta} onRefresh={() => void startRefresh()}
                            refreshing={refreshing} />
            <ConnectionScopePicker value={connectionId} onChange={setConnectionId} />
          </div>
        </div>
        {/* Nine tabs with real names do not fit at 1440px at the default `px-3 text-sm`.
            Scrolling horizontally keeps every label on one line and readable; wrapping them
            turned the bar into three ragged rows that pushed the content below the fold.
            Scrolling alone is not enough though — with four tabs off-screen and no edge cue, a
            live tenant looked like it simply had five screens. Sized to match IAM: the glyph is
            rendered at 11px rather than at label size, which is what makes the row fit at 1152
            instead of 1366. */}
        <div className="flex items-center gap-1">
          <TabScroller activeId={tab}>
            {ENTRA_NAV.map(({ id, label, icon }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                title={label}
                data-active={tab === id ? "true" : undefined}
                className={`shrink-0 whitespace-nowrap rounded-t-lg px-1 py-1.5 text-[13px] font-medium ${
                  tab === id ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
                }`}
              >
                <span aria-hidden="true" className="mr-0.5 text-[11px]">{icon}</span>
                {label}
              </button>
            ))}
          </TabScroller>
        </div>
      </div>

      {(refreshing || progress.length > 0) && (
        <ProgressStrip progress={progress} refreshing={refreshing} onDismiss={() => setProgress([])} />
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "posture" && (
          <PostureTab connectionId={cid} onRefresh={() => void startRefresh()} onOpenSetup={() => setTab("setup")} onOpenPillar={() => setTab("findings")} />
        )}
        {tab === "conditional-access" && <EntraCaView connectionId={cid} onOpenSetup={() => setTab("setup")} />}
        {tab === "privileged" && <EntraPrivilegedView connectionId={cid} onOpenSetup={() => setTab("setup")} />}
        {tab === "applications" && <EntraAppsView connectionId={cid} />}
        {tab === "signals" && (
          <EntraSignalsView connectionId={cid} onOpenSetup={() => setTab("setup")}
                            onRecollect={(domains) => void startRefresh(domains)} />
        )}
        {tab === "governance" && <EntraGovernanceView connectionId={cid} onOpenSetup={() => setTab("setup")} />}
        {tab === "graph" && <EntraGraphView connectionId={cid} onOpenSetup={() => setTab("setup")} />}
        {tab === "findings" && <EntraScannersView connectionId={cid} onOpenSetup={() => setTab("setup")} />}
        {tab === "investigate" && <EntraInvestigateView connectionId={cid ?? ""} />}
        {tab === "setup" && <EntraSetupView connectionId={cid} />}
      </div>
    </div>
  );
}

function ProgressStrip({
  progress,
  refreshing,
  onDismiss,
}: {
  progress: EntraProgress[];
  refreshing: boolean;
  onDismiss: () => void;
}) {
  const last = progress[progress.length - 1];
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b bg-indigo-50 px-4 py-1.5 text-xs text-indigo-900">
      <div className="flex items-center gap-2">
        <span className="font-medium">{refreshing ? "Collecting…" : "Collection finished"}</span>
        <span className="truncate text-indigo-700">{last?.message}</span>
        <button onClick={() => setOpen((v) => !v)} className="ml-auto underline underline-offset-2">
          {open ? "hide log" : `log (${progress.length})`}
        </button>
        {!refreshing && (
          <button onClick={onDismiss} className="text-indigo-500 hover:text-indigo-800">
            ✕
          </button>
        )}
      </div>
      {open && (
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-white/70 p-2 text-[11px] leading-relaxed">
          {progress.map((p) => `${p.level.toUpperCase().padEnd(5)} ${p.message}`).join("\n")}
        </pre>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------- posture
function PostureTab({
  connectionId,
  onRefresh,
  onOpenSetup,
  onOpenPillar,
}: {
  connectionId: string | null;
  onRefresh: () => void;
  onOpenSetup: () => void;
  onOpenPillar: (pillar: string) => void;
}) {
  const q = useQuery({
    queryKey: ["entra-posture", connectionId],
    queryFn: () => api.entraPosture(connectionId),
  });
  // Before every early return below — this component has three, and a hook after any of them
  // changes the hook order between renders.
  const download = useExportDownload("Entra export");

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading posture…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const data = q.data!;
  if (!data.meta.loaded) {
    return (
      <EntraEmpty
        kind="cold"
        detail="Nothing has been collected for this tenant yet. A refresh reads the directory read-only and never writes."
        onRefresh={onRefresh}
      />
    );
  }

  const s = data.score;
  const trend = data.trend as PostureTrend;
  const tenantName = data.tenant?.display_name || data.tenant?.primary_domain || data.meta.tenant_id;

  return (
    <div className="space-y-4 p-4">
      {download.dialog}
      <CoverageBanner meta={data.meta} onOpenSetup={onOpenSetup} />

      {/* Headline: the score is meaningless without its coverage, so they are never split. */}
      <div className="rounded-lg border bg-white p-4">
        <div className="flex flex-wrap items-center gap-5">
          <ScoreRing score={s.score} coverage={s.coverage} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-sm text-gray-500">
              <span>{tenantName}</span>
              {/* A federated tenant does not authenticate its own users, which changes how
                  every authentication figure below should be read. */}
              <FederationNote fabric={data.identity_fabric} context="inline" />
            </div>
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-2xl font-semibold text-gray-900">Identity posture {s.score}/100</span>
              {s.grade ? (
                <span className="rounded bg-gray-100 px-2 py-0.5 text-sm font-medium text-gray-700">
                  Grade {s.grade} · {s.grade_label}
                </span>
              ) : (
                <span className="text-xs text-amber-700">{s.grade_withheld_reason}</span>
              )}
              {data.trend.delta != null && data.trend.delta !== 0 && (
                <span className={`text-sm font-medium ${data.trend.delta > 0 ? "text-green-600" : "text-red-600"}`}>
                  {data.trend.delta > 0 ? "▲" : "▼"} {Math.abs(data.trend.delta)} point
                  {Math.abs(data.trend.delta) === 1 ? "" : "s"} since the previous run
                </span>
              )}
              {trend.points.length > 1 && (
                <Sparkline values={trend.points.map((p) => p.score)} w={110} h={26} />
              )}
            </div>
            <div className="mt-1 text-xs text-gray-500">
              Measured {Math.round(s.coverage * 100)}% of the model · {s.measured_signals} of {s.total_signals} checks ·
              registry v{s.registry_version}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {(["critical", "high", "medium", "low"] as const).map((sev) => (
                <span key={sev} className="inline-flex items-center gap-1 text-xs text-gray-600">
                  <SevBadge sev={sev} />
                  {s.findings_by_severity?.[sev] ?? 0}
                </span>
              ))}
              {/* One button for the whole section. It lives here rather than per-tab because
                  the workbook is not this tab's data — it is every tab's, plus the raw
                  directory the tabs only show counts of. The connection id is explicit: the
                  equivalent IAM link omitted it and silently exported the DEFAULT tenant.
                  Routed through fetch rather than a plain href so the ~8s build has visible
                  progress; without it people conclude it is broken and click again, which
                  starts a second build. */}
              <button
                type="button"
                onClick={() => download.start(api.entraWorkbookUrl(connectionId), "entra-identity-review.xlsx")}
                disabled={download.phase !== "idle"}
                className="ml-auto rounded border border-green-300 bg-green-50 px-2.5 py-1 text-xs font-medium text-green-700 hover:bg-green-100 disabled:opacity-50"
                title="Every /entra tab and sub-tab as one multi-sheet workbook — posture, findings, Conditional Access, privileged access, applications, sign-in risk, governance, blast radius and the raw directory. Contains personal data."
              >
                ⬇ Export everything to Excel
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Pillars */}
      <div className="rounded-lg border bg-white">
        <div className="border-b px-4 py-2 text-[13px] font-semibold text-gray-800">Pillars</div>
        <div className="divide-y">
          {s.pillars.map((p) => (
            <PillarRow
              key={p.key}
              pillar={p}
              history={trend.points.map((pt) => pt.pillars?.[p.key] ?? null)}
              delta={trend.pillar_delta?.[p.key]}
              onOpen={() => onOpenPillar(p.key)}
              onOpenSetup={onOpenSetup}
            />
          ))}
        </div>
      </div>

      {/* Recoverable points — falls out of the score model for free and is the most useful
          single list on the page. */}
      {s.top_wins.length > 0 && (
        <div className="rounded-lg border bg-white">
          <div className="border-b px-4 py-2 text-[13px] font-semibold text-gray-800">Biggest wins available</div>
          <div className="divide-y">
            {s.top_wins.map((w) => (
              <div key={w.signal_id} className="flex items-start gap-3 px-4 py-2.5">
                <span className="mt-0.5 w-12 shrink-0 text-right text-sm font-semibold text-green-700">
                  +{w.points.toFixed(1)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <SevBadge sev={w.severity} />
                    <span className="text-[13px] font-medium text-gray-900">{w.title}</span>
                    <span className="text-xs text-gray-400">({w.findings})</span>
                  </div>
                  <div className="mt-0.5 text-xs text-gray-600">{w.remediation}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trend */}
      {trend.points.length > 1 ? (
        <Trend points={trend.points} pillars={s.pillars} />
      ) : (
        <div className="rounded-lg border border-dashed bg-white p-3 text-xs text-gray-500">
          Only one full collection has been recorded for this tenant, so there is nothing to
          trend yet. A history point is written after each successful full refresh, and the
          last 90 are kept.
        </div>
      )}

      {/* Inventory counts */}
      <div className="grid gap-3 md:grid-cols-4">
        {Object.entries(data.counts ?? {}).map(([domain, counts]) => (
          <div key={domain} className="rounded-lg border bg-white p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">{domain}</div>
            <div className="mt-1 space-y-0.5 text-[13px]">
              {Object.entries(counts ?? {}).slice(0, 6).map(([k, v]) => (
                <div key={k} className="flex justify-between">
                  <span className="text-gray-500">{k.replace(/_/g, " ")}</span>
                  <span className="font-medium text-gray-800">{Number(v).toLocaleString()}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Posture history as served by `/api/entra/posture`. */
type PostureTrendPoint = EntraPosture["trend"]["points"][number];
type PostureTrend = EntraPosture["trend"];

const SERIES_COLOURS = ["#0891b2", "#ea580c", "#7c3aed", "#16a34a", "#db2777", "#ca8a04", "#0284c7", "#dc2626"];

/**
 * A score movement, or nothing at all.
 *
 * Zero renders as "level" rather than "▲ 0", and a pillar that was blind on either side of
 * the comparison renders as nothing: "unchanged" and "we could not see it" are different
 * answers and a chip that conflates them is worse than no chip.
 */
function DeltaChip({ delta, since, compact }: { delta?: number | null; since?: string | null; compact?: boolean }) {
  if (delta == null) return null;
  const suffix = since ? ` since ${since.slice(0, 10)}` : " since the previous run";
  if (delta === 0) {
    return (
      <span className="text-[11px] text-gray-400" title={`No change${suffix}`}>
        {compact ? "–" : `level${suffix}`}
      </span>
    );
  }
  const up = delta > 0;
  return (
    <span className={`text-[11px] font-medium ${up ? "text-green-600" : "text-red-600"}`}
          title={`${up ? "Up" : "Down"} ${Math.abs(delta)} point${Math.abs(delta) === 1 ? "" : "s"}${suffix}`}>
      {up ? "▲" : "▼"} {Math.abs(delta)}
      {!compact && ` point${Math.abs(delta) === 1 ? "" : "s"}${suffix}`}
    </span>
  );
}

/**
 * A pillar's own history, drawn small.
 *
 * Scaled to the series rather than to 0–100: every pillar on a settled tenant would
 * otherwise be a flat line, which is exactly the movement this was added to show. The band
 * is never narrower than ten points, so a one-point wobble cannot masquerade as a collapse.
 */
function Sparkline({ values, tone = "#4f46e5", w = 88, h = 24 }: {
  values: (number | null)[]; tone?: string; w?: number; h?: number;
}) {
  const runs = segments(values);
  const seen = values.filter((v): v is number => v != null);
  if (seen.length < 2) {
    return (
      <span className="text-[10px] text-gray-300" title="Not enough history yet — trend appears after two full collections">
        no trend yet
      </span>
    );
  }
  const lo = Math.min(...seen), hi = Math.max(...seen);
  const mid = (lo + hi) / 2;
  const band = Math.max(10, hi - lo);
  const top = Math.min(100, mid + band / 2), bottom = Math.max(0, mid - band / 2);
  const x = (i: number) => (values.length > 1 ? (i / (values.length - 1)) * w : w / 2);
  const y = (v: number) => 2 + (1 - (v - bottom) / Math.max(1, top - bottom)) * (h - 4);
  const last = seen[seen.length - 1];
  const first = seen[0];
  const colour = last > first ? "#16a34a" : last < first ? "#dc2626" : tone;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} className="shrink-0"
         role="img"
         aria-label={`Trend from ${first} to ${last}`}
         >
      <title>{`${seen.length} recorded refreshes · ${first} → ${last}`}</title>
      {runs.map((seg, i) => (
        <path key={i} fill="none" stroke={colour} strokeWidth={1.5} strokeLinecap="round"
              d={seg.map((p, j) => `${j === 0 ? "M" : "L"} ${x(p.i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ")} />
      ))}
      <circle cx={x(values.length - 1)} cy={y(last)} r={1.8} fill={colour} />
    </svg>
  );
}

function PillarRow({
  pillar,
  history,
  delta,
  onOpen,
  onOpenSetup,
}: {
  pillar: EntraPillar;
  history: (number | null)[];
  delta?: number;
  onOpen: () => void;
  onOpenSetup: () => void;
}) {
  const measured = pillar.score != null;
  const tone =
    pillar.score == null
      ? "bg-gray-300"
      : pillar.score >= 80
      ? "bg-green-500"
      : pillar.score >= 60
      ? "bg-amber-500"
      : "bg-red-500";
  const reason = pillar.reason || pillar.not_measured[0]?.reason || "";
  return (
    <div className="flex items-center gap-3 px-4 py-2.5">
      <div className="w-52 shrink-0">
        <div className="text-[13px] font-medium text-gray-900">{pillar.label}</div>
        <div className="text-[11px] text-gray-400">weight {pillar.weight}</div>
      </div>
      <div className="w-40 shrink-0">
        <Bar value={pillar.score ?? 0} tone={tone} />
      </div>
      <div className="w-12 shrink-0 text-right text-sm font-semibold text-gray-800">
        {measured ? pillar.score : "—"}
      </div>
      <div className="flex w-28 shrink-0 flex-col items-end gap-0.5">
        <Sparkline values={history} />
        <DeltaChip delta={delta} compact />
      </div>
      <div className="w-28 shrink-0">
        <StateChip state={pillar.state} title={reason} />
      </div>
      <div className="min-w-0 flex-1 text-xs text-gray-500">
        {measured ? (
          <>
            {pillar.findings} finding{pillar.findings === 1 ? "" : "s"} · measured{" "}
            {pillar.measured_signals}/{pillar.total_signals} checks
            {pillar.measured_fraction < 1 && ` (${Math.round(pillar.measured_fraction * 100)}% of this pillar)`}
          </>
        ) : (
          <span title={reason}>{reason || "Not measured."}</span>
        )}
      </div>
      {measured ? (
        <button onClick={onOpen} className="shrink-0 text-xs font-medium text-brand underline underline-offset-2">
          View findings
        </button>
      ) : (
        <button onClick={onOpenSetup} className="shrink-0 text-xs font-medium text-brand underline underline-offset-2">
          Fix coverage
        </button>
      )}
    </div>
  );
}

function Trend({ points, pillars }: { points: PostureTrendPoint[]; pillars: EntraPillar[] }) {
  // Every pillar is drawn by default: the first question on this card is "what moved",
  // which needs all eight visible at once. Chips then subtract, so isolating one pillar is
  // still a click away.
  const [shown, setShown] = useState<string[]>(() => pillars.map((p) => p.key));
  const toggle = (key: string) =>
    setShown((s) => (s.includes(key) ? s.filter((k) => k !== key) : [...s, key]));
  const allShown = shown.length === pillars.length;

  // Color is fixed to the pillar's position in the model, not to its position in the
  // selection. Keying it off the selection meant a pillar changed color as its neighbours
  // were toggled, and the chip and its line could disagree about which color it was.
  const colourOf = (key: string) =>
    SERIES_COLOURS[Math.max(0, pillars.findIndex((p) => p.key === key)) % SERIES_COLOURS.length];

  const series = [
    { key: "__overall", label: "Overall", colour: "#4f46e5",
      values: points.map((p) => p.score as number | null) },
    ...pillars
      .filter((p) => shown.includes(p.key))
      .map((p) => ({
        key: p.key, label: p.label, colour: colourOf(p.key),
        values: points.map((pt) => pt.pillars?.[p.key] ?? null),
      })),
  ];

  const w = 600, h = 120, pad = 2;
  const x = (i: number) => (points.length > 1 ? (i / (points.length - 1)) * w : w / 2);
  const y = (v: number) => pad + (1 - v / 100) * (h - pad * 2);

  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold text-gray-800">Trend</span>
        <span className="text-[11px] text-gray-400">
          {points.length} refresh{points.length === 1 ? "" : "es"} recorded · one point per full collection
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          <button
            onClick={() => setShown(allShown ? [] : pillars.map((p) => p.key))}
            title={allShown ? "Show the tenant score on its own" : "Overlay every pillar"}
            className="rounded-full border px-2 py-0.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50"
          >
            {allShown ? "Overall only" : "Select all"}
          </button>
          {pillars.map((p) => {
            const on = shown.includes(p.key);
            return (
              <button
                key={p.key}
                onClick={() => toggle(p.key)}
                title={on ? `Hide ${p.label}` : `Overlay ${p.label}`}
                className={`rounded-full border px-2 py-0.5 text-[11px] ${
                  on ? "border-transparent text-white" : "text-gray-500 hover:bg-gray-50"}`}
                style={on ? { background: colourOf(p.key) } : undefined}
              >
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      <svg viewBox={`0 0 ${w} ${h}`} className="h-32 w-full" preserveAspectRatio="none">
        {/* A score chart without a fixed 0–100 frame flatters noise into a trend. */}
        {[0, 25, 50, 75, 100].map((v) => (
          <line key={v} x1={0} x2={w} y1={y(v)} y2={y(v)}
                stroke={v === 0 || v === 100 ? "#e2e8f0" : "#f1f5f9"} strokeWidth={1} />
        ))}
        {series.map((s) => (
          <g key={s.key}>
            {segments(s.values).map((seg, i) => (
              <path
                key={i}
                d={seg.map((pt, j) => `${j === 0 ? "M" : "L"} ${x(pt.i).toFixed(1)} ${y(pt.v).toFixed(1)}`).join(" ")}
                fill="none" stroke={s.colour} strokeWidth={s.key === "__overall" ? 2 : 1.6}
                strokeLinecap="round"
              />
            ))}
          </g>
        ))}
      </svg>

      <div className="flex items-center justify-between text-[11px] text-gray-400">
        <span>{points[0]?.at?.slice(0, 10)}</span>
        <span className="flex flex-wrap items-center gap-3">
          {series.map((s) => {
            const last = [...s.values].reverse().find((v) => v != null);
            return (
              <span key={s.key} className="flex items-center gap-1 text-gray-600">
                <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.colour }} />
                {s.label} {last ?? "—"}
              </span>
            );
          })}
        </span>
        <span>{points[points.length - 1]?.at?.slice(0, 10)}</span>
      </div>
      <div className="mt-1 text-[11px] text-gray-400">
        A gap in a line is a refresh where that pillar could not be measured — not a score of zero.
      </div>
    </div>
  );
}

/** Split a series into unbroken runs, so a blind refresh leaves a gap instead of a cliff. */
function segments(values: (number | null)[]): { i: number; v: number }[][] {
  const out: { i: number; v: number }[][] = [];
  let run: { i: number; v: number }[] = [];
  values.forEach((v, i) => {
    if (v == null) { if (run.length) out.push(run); run = []; return; }
    run.push({ i, v });
  });
  if (run.length) out.push(run);
  // A single measured point has no line to draw, but it should still show as a dot-sized run.
  return out.map((r) => (r.length === 1 ? [r[0], r[0]] : r));
}

