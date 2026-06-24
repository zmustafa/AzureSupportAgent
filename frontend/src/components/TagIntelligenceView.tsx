/**
 * Tag Intelligence — an AI-powered tag command center over the Azure estate (F1-F12).
 *
 * One screen, seven URL-routed tabs (/tagintel/:tab) sharing a single scope header
 * (Workload | Subscription, identical to the Performance Profiler) and a server-side cache:
 * the heavy Resource Graph scan is shared with Inventory, runs in a module-level background
 * registry so it keeps going when you navigate away, and every tab reads the cached result.
 *
 * Tabs: Census (F1+F10) · Hygiene (F2+F3) · Coverage (F6) · Cost (F4+F5) · Drift (F7) ·
 * Policy (F8) · Remediate (F9+F11). Read-only everywhere except Remediate, which only ever
 * produces a plan + scripts (never writes to Azure).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ResponsiveContainer, Treemap, RadialBarChart, RadialBar, PolarAngleAxis,
  BarChart, Bar, XAxis, YAxis, Tooltip, AreaChart, Area, CartesianGrid, Cell,
} from "recharts";
import {
  api, type TagScopeSel, type TagCensus, type TagCatalogEntry, type TagKeyCluster,
  type TagValueCluster, type TagCoverageResponse, type TagCostResponse, type TagBillingMapResponse,
  type TagDriftDiff, type TagPolicyDefinition, type TagRemediationOp, type TagRemediationPlan,
  type TagChangeSet, type TagChangeSetGroup, type TagChangeSetBundle, type TagChangeSetImportResult,
  type TagGeneratedOp,
  streamTagintelRemediateApply, type TagApplyStart, type TagApplyResult,
} from "../api";
import { ScopePicker, type ScopeKind } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { PageIntro } from "./PageIntro";
import { TAGINTEL_NAV, type TagIntelTab } from "./navConfig";
import { formatError } from "../utils/format";
import { TagRevisionsPanel } from "./ownership/OwnerImportTags";
import { ChangeSetFlow } from "./tagintel/ChangeSetFlow";
import { TagKeyDrillGrid } from "./tagintel/TagKeyDrillGrid";
import { isRefreshing, startBackgroundRefresh, takeRefreshError, useBackgroundRefresh } from "../utils/backgroundRefresh";

const CAT_COLORS: Record<string, string> = {
  billing: "#2563eb", ownership: "#7c3aed", environment: "#0891b2", application: "#16a34a",
  organization: "#d97706", security: "#dc2626", lifecycle: "#db2777", operations: "#0d9488", other: "#64748b",
};
const PALETTE = ["#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#dc2626", "#db2777", "#0d9488", "#64748b", "#9333ea"];
const CONF_STYLE: Record<string, string> = {
  confirmed: "bg-emerald-100 text-emerald-700", high: "bg-blue-100 text-blue-700",
  medium: "bg-amber-100 text-amber-700", low: "bg-gray-200 text-gray-600",
};

// --------------------------------------------------------------- Hygiene → Remediate handoff
// localStorage keys the Remediate builder persists to (one source of truth, shared with the
// "Fix in Remediate" handoff so the two never drift).
const CS_PERSIST = {
  name: "azsup.tagintel.cs.name",
  desc: "azsup.tagintel.cs.desc",
  ops: "azsup.tagintel.cs.ops",
  loadedId: "azsup.tagintel.cs.loadedId",
} as const;

// Stage a change-set into the Remediate builder's persisted state, then the caller navigates to
// the Remediate tab — which mounts fresh and reads exactly these keys. Writing localStorage (vs.
// an in-memory hand-off) is robust under React StrictMode's mount→unmount→remount.
function stageChangeSetToBuilder(name: string, description: string, ops: TagRemediationOp[]) {
  try {
    localStorage.setItem(CS_PERSIST.ops, JSON.stringify(ops));
    localStorage.setItem(CS_PERSIST.name, JSON.stringify(name));
    localStorage.setItem(CS_PERSIST.desc, JSON.stringify(description));
    localStorage.setItem(CS_PERSIST.loadedId, JSON.stringify(""));   // a fresh, unsaved draft
  } catch { /* ignore unavailable storage */ }
}

// Build the tag operations that resolve a hygiene finding.
// A near-duplicate key cluster → rename every variant key to the canonical key.
function opsForKeyCluster(c: TagKeyCluster): TagRemediationOp[] {
  return c.members.filter((m) => m && m !== c.canonical).map((m) => ({ type: "rename_key", key: m, to_key: c.canonical }));
}
// A value-variant cluster → normalize every off-canonical value to the canonical value.
function opsForValueCluster(c: TagValueCluster): TagRemediationOp[] {
  const ops: TagRemediationOp[] = [];
  for (const v of c.variants) {
    for (const m of v.members) {
      if (m && m !== v.canonical) ops.push({ type: "normalize_value", key: c.key, from_value: m, to_value: v.canonical });
    }
  }
  return ops;
}

// --------------------------------------------------------------- background scan registry
// A Census refresh is the one heavy op (it triggers the shared Resource Graph scan). It runs
// through the shared background-refresh registry (keyed "tagintel:<scopeKey>") so it keeps
// running — and auto-surfaces — even if the user switches tabs or navigates away, exactly like
// the Monitoring / Telemetry / Backup-DR coverage screens and the Performance Profiler.
function scanKey(scopeKey: string): string { return `tagintel:${scopeKey}`; }


function fmtMoney(n: number | undefined, currency?: string): string {
  if (n === undefined || n === null) return "—";
  const cur = currency || "USD";
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(n); }
  catch { return `${Math.round(n).toLocaleString()} ${cur}`; }
}
function fmtAge(seconds?: number): string {
  if (!seconds && seconds !== 0) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function CopyBtn({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => { void navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); }}
      className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50"
    >{done ? "✓ Copied" : label}</button>
  );
}

function Chip({ kind }: { kind: string }) {
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${CONF_STYLE[kind] || "bg-gray-100 text-gray-600"}`}>{kind}</span>;
}

function NotLoaded({ onLoad, onLoadCache, busy }: { onLoad?: () => void; onLoadCache?: () => void; busy?: boolean }) {
  return (
    <div className="m-6 rounded-xl border border-dashed bg-gray-50 p-8 text-center">
      <div className="text-3xl">🏷️</div>
      <p className="mt-2 text-sm font-medium text-gray-700">No tag data cached for this scope yet.</p>
      <p className="mt-1 text-xs text-gray-500">Tag Intelligence shares the Inventory scan. <b>Load</b> reads from cache (instant); <b>Refresh</b> runs a fresh Resource Graph scan and keeps running if you navigate away.</p>
      <div className="mt-4 flex items-center justify-center gap-2">
        {onLoadCache && (
          <button onClick={onLoadCache} disabled={busy} className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">⤓ Load from cache</button>
        )}
        {onLoad && (
          <button onClick={onLoad} disabled={busy} className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50">
            {busy ? "Refreshing…" : "↻ Refresh from Azure"}
          </button>
        )}
      </div>
    </div>
  );
}

// Prompt shown when a scope is selected but not yet loaded — mirrors the coverage screens'
// "Load coverage" gate so a scope switch never auto-hits Azure.
function LoadPrompt({ onLoad, onRefresh, scanning, scopeKind }: { onLoad: () => void; onRefresh: () => void; scanning: boolean; scopeKind: ScopeKind }) {
  return (
    <div className="m-6 rounded-xl border border-dashed bg-gray-50 p-8 text-center">
      <div className="text-3xl">🏷️</div>
      <p className="mt-2 text-sm font-medium text-gray-700">Ready to analyze this {scopeKind === "workload" ? "workload" : "subscription"}.</p>
      <p className="mt-1 text-xs text-gray-500"><b>Load</b> reads the last cached scan instantly (no Azure call). <b>Refresh</b> runs a fresh Resource Graph scan — it keeps running in the background if you navigate away.</p>
      <div className="mt-4 flex items-center justify-center gap-2">
        <button onClick={onLoad} disabled={scanning} className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">⤓ Load from cache</button>
        <button onClick={onRefresh} disabled={scanning} className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50">{scanning ? "Refreshing…" : "↻ Refresh from Azure"}</button>
      </div>
    </div>
  );
}

// =====================================================================================
export function TagIntelligencePanel({ tab = "census" }: { tab?: TagIntelTab }) {
  const qc = useQueryClient();
  const [scopeKind, setScopeKind] = usePersistedState<ScopeKind>("azsup.tagintel.scopeKind", "subscription");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.tagintel.workloadId", "");
  const [subId, setSubId] = usePersistedState("azsup.tagintel.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.tagintel.subName", "");
  const [connId, setConnId] = usePersistedState("azsup.tagintel.connId", "");
  useWorkloadDeepLink(setScopeKind, setWorkloadId);

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = workloadsQ.data?.workloads ?? [];
  const effWorkloadId = scopeKind === "workload" ? (workloadId || workloads[0]?.id || "") : "";
  const enabled = scopeKind === "workload" ? !!effWorkloadId : !!subId;
  const scopeKey = `${scopeKind}:${effWorkloadId || subId}:${connId}`;
  const sel: TagScopeSel = scopeKind === "workload"
    ? { workload_id: effWorkloadId, connection_id: connId }
    : { scope: subId ? `sub:${subId}` : "", connection_id: connId };

  // Background refresh (per-scope) — a forced Resource Graph scan that survives scope switches
  // and navigation, via the shared registry the coverage screens use.
  const refreshKey = scanKey(scopeKey);
  const refreshVersion = useBackgroundRefresh();
  const scanning = isRefreshing(refreshKey);
  const [scanErr, setScanErr] = useState("");

  // Load gating: the census (and every tab) reads from cache ONLY once the user has Loaded this
  // scope. Persisted so returning to a previously-loaded scope re-reads cache without a click —
  // and a background Refresh that finished while away surfaces on return. "Load" = cache only
  // (no Azure); "Refresh" = a fresh scan.
  const [loadedScope, setLoadedScope] = usePersistedState<string>("azsup.tagintel.loadedScope", "");
  const active = enabled && loadedScope === scopeKey;

  // Census drives the shared cache; load it once per scope, then every tab reads the cache.
  const censusQ = useQuery({
    queryKey: ["tagintel", "census", scopeKey],
    queryFn: () => api.tagintelCensus(sel, false),
    enabled: active,
  });
  const loaded = !!censusQ.data?.available;

  function loadCache() {
    if (enabled) { setScanErr(""); setLoadedScope(scopeKey); void qc.invalidateQueries({ queryKey: ["tagintel", "census", scopeKey] }); }
  }

  function refresh() {
    if (!enabled || scanning) return;
    setScanErr("");
    setLoadedScope(scopeKey);
    const s = sel;
    startBackgroundRefresh(refreshKey, async () => {
      await api.tagintelCensus(s, true);                       // force a fresh Resource Graph scan
      await qc.invalidateQueries({ queryKey: ["tagintel"] });  // every tab re-reads the fresh cache
    });
  }

  // Surface an error from a background refresh that finished (possibly while the user was away).
  useEffect(() => {
    if (!scanning) {
      const err = takeRefreshError(refreshKey);
      if (err) setScanErr(err);
    }
  }, [refreshVersion, refreshKey, scanning]);

  const navItem = TAGINTEL_NAV.find((n) => n.id === tab) ?? TAGINTEL_NAV[0];
  const fetchedAge = censusQ.data?.age_seconds;


  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Header: title + scope + cache freshness */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-lg">🏷️</span>
            <h1 className="text-base font-semibold text-gray-900">Tag Intelligence</h1>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {(loaded || scanning) && (
              <span className="flex items-center gap-1.5 rounded-full bg-gray-100 px-2.5 py-1 text-[11px] text-gray-600">
                <span className={`inline-block h-1.5 w-1.5 rounded-full ${scanning ? "animate-pulse bg-amber-500" : "bg-emerald-500"}`} />
                {scanning ? "Refreshing… (keeps running if you navigate away)" : `Cached ${fmtAge(fetchedAge)}`}
              </span>
            )}
            <ConnectionScopePicker value={connId} onChange={(id) => { setConnId(id); if (scopeKind === "subscription") { setSubId(""); setSubName(""); } }} />
            <ScopePicker
              scopeKind={scopeKind}
              onScopeKindChange={setScopeKind}
              workloads={workloads}
              workloadId={effWorkloadId}
              onWorkloadChange={setWorkloadId}
              subId={subId}
              subName={subName}
              connectionId={connId}
              onSubPick={(id, name) => { setSubId(id); setSubName(name); }}
            />
            <button
              onClick={loadCache}
              disabled={!enabled || scanning}
              title="Load this scope from cache — instant, no Azure scan"
              className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >⤓ Load</button>
            <button
              onClick={refresh}
              disabled={!enabled || scanning}
              title="Force a fresh Resource Graph scan (runs in the background)"
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >{scanning ? "Refreshing…" : "↻ Refresh"}</button>
          </div>
        </div>
        {scanErr && (
          <div className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">{scanErr}</div>
        )}
        {/* Tab strip */}
        <div className="mt-3 flex flex-wrap gap-1">
          {TAGINTEL_NAV.map((n) => (
            <Link
              key={n.id}
              to={n.id === "census" ? "/tagintel" : `/tagintel/${n.id}`}
              className={`rounded-lg px-3 py-1.5 text-sm transition ${tab === n.id ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-100"}`}
            >{n.label}</Link>
          ))}
        </div>
      </div>

      <div className="relative min-h-0 flex-1">
        {/* While a background refresh runs, veil the tab BODY only (the tabs/header above stay
            interactive) so the data underneath can't be misread as final. The veil sits on this
            NON-scrolling wrapper (a sibling of the scroll area) so it stays pinned to the visible
            viewport even when the user has scrolled down — e.g. to the Remediate apply controls. */}
        {scanning && enabled && (
          <div className="pointer-events-none absolute inset-0 z-20 flex items-start justify-center bg-white/60 backdrop-blur-[1px]">
            <div className="mt-24 flex items-center gap-2 rounded-full border border-amber-200 bg-white px-4 py-2 text-sm font-medium text-amber-700 shadow-sm">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              Refreshing…
            </div>
          </div>
        )}
        <div className="h-full overflow-auto">
        <div className="px-5 pt-3">
          <PageIntro title={navItem.label.replace(/^\S+\s/, "")} blurb={navItem.description} storageKey={`tagintel-${tab}`} />
        </div>

        {!enabled ? (
          <div className="m-6 rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-600">
            Pick a {scopeKind === "workload" ? "workload" : "subscription"} above to begin.
          </div>
        ) : tab === "policy" ? (
          <div className="px-5 pb-10"><PolicyTab /></div>
        ) : !active ? (
          <LoadPrompt onLoad={loadCache} onRefresh={refresh} scanning={scanning} scopeKind={scopeKind} />
        ) : censusQ.isLoading ? (
          <div className="p-8 text-center text-sm text-gray-500">Loading from cache…</div>
        ) : !loaded && tab !== "remediate" ? (
          <NotLoaded onLoad={refresh} onLoadCache={loadCache} busy={scanning} />
        ) : (
          <div className="px-5 pb-10">
            {tab === "census" && <CensusTab census={censusQ.data?.census} sel={sel} truncated={censusQ.data?.truncated} cap={censusQ.data?.estate_cap} />}
            {tab === "hygiene" && <HygieneTab sel={sel} scopeKey={scopeKey} />}
            {tab === "coverage" && <CoverageTab sel={sel} scopeKey={scopeKey} />}
            {tab === "cost" && <CostTab sel={sel} scopeKey={scopeKey} />}
            {tab === "drift" && <DriftTab sel={sel} scopeKey={scopeKey} />}
            {tab === "generate" && <GenerateTab sel={sel} loaded={loaded} />}
            {tab === "remediate" && <RemediateTab sel={sel} loaded={loaded} census={censusQ.data?.census} onRefreshScope={refresh} scanning={scanning} />}
          </div>
        )}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- KPI
function Kpi({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: string; tone?: string }) {
  return (
    <div className="rounded-xl border bg-white p-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone || "text-gray-900"}`}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500">{sub}</div>}
    </div>
  );
}

// ============================================================ CENSUS (F1 + F10)
function CensusTab({ census, sel, truncated, cap }: { census?: TagCensus; sel: TagScopeSel; truncated?: boolean; cap?: number }) {
  // Bridge: the drill grid's "use as filter" pushes a question into the Ask console.
  const askPrefill = useRef<((text: string) => void) | null>(null);
  if (!census) return null;
  const keys = census.keys;
  const treemap = census.scope_coverage.by_subscription.map((s, i) => ({ name: s.name, size: s.total, coverage: s.coverage_pct, fill: PALETTE[i % PALETTE.length] }));

  return (
    <div className="space-y-4">
      {truncated && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Estate exceeds {cap?.toLocaleString()} resources — analysis is on a bounded sample. Narrow the scope for full fidelity.
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Kpi label="Resources" value={census.total_resources.toLocaleString()} />
        <Kpi label="Tag coverage" value={`${census.tag_coverage_pct}%`} tone={census.tag_coverage_pct < 50 ? "text-red-600" : census.tag_coverage_pct < 80 ? "text-amber-600" : "text-emerald-600"} />
        <Kpi label="Untagged" value={census.untagged_count.toLocaleString()} tone={census.untagged_count ? "text-amber-600" : "text-gray-900"} />
        <Kpi label="Distinct keys" value={census.distinct_keys} />
        <Kpi label="High-cardinality" value={census.flags.high_cardinality} sub="noisy keys" />
        <Kpi label="Single-sub keys" value={census.flags.single_subscription} sub="scoped to 1 sub" />
      </div>

      {/* AI ask console (F10) */}
      <AskConsole sel={sel} registerPrefill={(fn) => { askPrefill.current = fn; }} />

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Key explorer (F1) — Power-BI-style expandable drill: key → value → sub → type → resource */}
        <div className="lg:col-span-2">
          <TagKeyDrillGrid keys={keys} sel={sel} onUseFilter={(text) => askPrefill.current?.(text)} />
        </div>

        {/* Coverage treemap */}
        <div className="space-y-4">
          <div className="rounded-xl border bg-white p-4">
            <div className="mb-1 text-sm font-medium text-gray-700">Coverage by subscription</div>
            <ResponsiveContainer width="100%" height={170}>
              <Treemap data={treemap} dataKey="size" stroke="#fff" content={<CoverageCell />} />
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
}

function CoverageCell(props: any) {
  const { x, y, width, height, name, coverage } = props;
  if (width < 8 || height < 8) return null;
  const c = coverage >= 80 ? "#16a34a" : coverage >= 50 ? "#d97706" : "#dc2626";
  return (
    <g>
      <rect x={x} y={y} width={width} height={height} style={{ fill: c, stroke: "#fff", opacity: 0.85 }} />
      {width > 50 && height > 24 && (
        <text x={x + 4} y={y + 16} fill="#fff" fontSize={11}>{name} · {coverage}%</text>
      )}
    </g>
  );
}

function AskConsole({ sel, registerPrefill }: { sel: TagScopeSel; registerPrefill?: (fn: (text: string) => void) => void }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<Awaited<ReturnType<typeof api.tagintelAsk>> | null>(null);
  const [busy, setBusy] = useState(false);
  // Let a parent (the drill grid's "use as filter") push a question into the box + focus it.
  useEffect(() => { registerPrefill?.((text: string) => { setQ(text); }); }, [registerPrefill]);
  async function ask(question: string) {
    setBusy(true); setQ(question);
    try { setRes(await api.tagintelAsk({ question, ...sel })); } catch (e) { setRes({ kind: "error", answer: formatError(e) }); } finally { setBusy(false); }
  }
  const suggestions = ["show all tag keys", "values for Environment", "resources missing Owner", "untagged resources", "high-cardinality tags", "VMs missing Owner tag", "storage accounts without Environment=prod"];
  return (
    <div className="rounded-xl border bg-gradient-to-br from-violet-50 to-white p-4">
      <div className="flex items-center gap-2">
        <span className="text-base">✨</span>
        <span className="text-sm font-medium text-gray-800">Ask about your tags</span>
        <span className="text-[11px] text-gray-400">natural language → Resource Graph</span>
      </div>
      <div className="mt-2 flex gap-2">
        <input
          value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && q.trim()) void ask(q); }}
          placeholder="e.g. which resources are missing Owner?"
          className="flex-1 rounded-lg border px-3 py-2 text-sm"
        />
        <button onClick={() => q.trim() && ask(q)} disabled={busy} className="rounded-lg bg-violet-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "…" : "Ask"}</button>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {suggestions.map((s) => <button key={s} onClick={() => void ask(s)} className="rounded-full border bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">{s}</button>)}
      </div>
      {/* While a new ask is in flight, hide the previous result so a stale grid isn't shown as
          if it answered the new question. */}
      {busy && (
        <div className="mt-3 flex items-center gap-2 rounded-lg border bg-white p-3 text-sm text-violet-700">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500" />
          Thinking… translating your question to a Resource Graph query.
        </div>
      )}
      {res && !busy && (
        <div className="mt-3 rounded-lg border bg-white p-3">
          <p className="text-sm text-gray-800">{res.answer}</p>
          {Array.isArray(res.data) && res.data.length > 0 && (
            <AskResultTable data={res.data as unknown[]} questionKind={res.kind} />
          )}
          {res.generated_query && (
            <div className="mt-2">
              <div className="mb-1 flex items-center gap-2">
                <span className="text-[11px] font-medium text-gray-500">Generated Resource Graph query</span>
                {res.source === "ai" && <span className="rounded-full bg-violet-100 px-1.5 py-0.5 text-[9px] font-medium text-violet-700">✨ AI-generated</span>}
                <CopyBtn text={res.generated_query} />
              </div>
              <pre className="overflow-auto rounded bg-gray-900 p-2 text-[11px] text-emerald-300">{res.generated_query}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Dynamic result table for the NL console — infers columns from the row objects (union of keys,
// first-seen order), formats headers/values, right-aligns numbers, truncates long ids, and offers
// copy/CSV. Falls back to a value list when the rows are primitives.
function AskResultTable({ data, questionKind }: { data: unknown[]; questionKind?: string }) {
  const [showAll, setShowAll] = useState(false);
  const rows = data.filter((d) => d != null);
  if (rows.length === 0) return null;

  const primitive = typeof rows[0] !== "object";
  const objs: Record<string, unknown>[] = primitive
    ? rows.map((v) => ({ value: v }))
    : (rows as Record<string, unknown>[]);

  // Column order: union of keys (first-seen), but push id-like + bulky columns to the end.
  const cols: string[] = [];
  for (const o of objs) for (const k of Object.keys(o)) if (!cols.includes(k)) cols.push(k);
  const weight = (c: string) => (c === "id" || c === "resourceId" ? 3 : c === "subscription_id" || c === "subscriptionId" ? 2 : c.toLowerCase().includes("group") ? 1 : 0);
  cols.sort((a, b) => weight(a) - weight(b));

  const limit = showAll ? objs.length : 12;
  const shown = objs.slice(0, limit);

  function fmtHeader(c: string): string {
    return c.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^\w/, (m) => m.toUpperCase());
  }
  function isNumCol(c: string): boolean { return objs.every((o) => o[c] == null || typeof o[c] === "number"); }
  function cell(v: unknown): string {
    if (v == null) return "—";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
  function toCsv(): string {
    const head = cols.map((c) => `"${fmtHeader(c)}"`).join(",");
    const body = objs.map((o) => cols.map((c) => `"${cell(o[c]).replace(/"/g, '""')}"`).join(","));
    return [head, ...body].join("\n");
  }
  function download() {
    const blob = new Blob([toCsv()], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `tag-query-${questionKind || "result"}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  const idCol = cols.find((c) => c === "id" || c === "resourceId");

  return (
    <div className="mt-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[11px] font-medium text-gray-500">{objs.length} row{objs.length === 1 ? "" : "s"}</span>
        <button onClick={() => { void navigator.clipboard.writeText(toCsv()); }} className="rounded border px-2 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50">Copy CSV</button>
        <button onClick={download} className="rounded border px-2 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50">⬇ CSV</button>
      </div>
      <div className="max-h-64 overflow-auto rounded border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-gray-50 text-left text-[10px] uppercase text-gray-400">
            <tr>{cols.map((c) => <th key={c} className={`px-2 py-1.5 ${isNumCol(c) ? "text-right" : ""}`}>{fmtHeader(c)}</th>)}</tr>
          </thead>
          <tbody>
            {shown.map((o, i) => (
              <tr key={i} className="border-t hover:bg-gray-50" title={idCol ? cell(o[idCol]) : undefined}>
                {cols.map((c) => {
                  const isId = c === "id" || c === "resourceId";
                  return (
                    <td key={c} className={`px-2 py-1 ${isNumCol(c) ? "text-right tabular-nums text-gray-700" : "text-gray-600"} ${isId ? "max-w-[280px] truncate font-mono text-[10px] text-gray-400" : ""}`}>
                      {c === "category" ? <span className="rounded px-1.5 py-0.5 text-[10px] text-white" style={{ background: CAT_COLORS[String(o[c])] || "#64748b" }}>{cell(o[c])}</span> : cell(o[c])}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {objs.length > 12 && (
        <button onClick={() => setShowAll((v) => !v)} className="mt-1 text-[11px] text-brand hover:underline">
          {showAll ? "Show less" : `Show all ${objs.length} rows`}
        </button>
      )}
    </div>
  );
}

// ============================================================ HYGIENE (F2 + F3)
function HygieneTab({ sel, scopeKey }: { sel: TagScopeSel; scopeKey: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const hygieneQ = useQuery({ queryKey: ["tagintel", "hygiene", scopeKey], queryFn: () => api.tagintelHygiene(sel) });
  const catalogQ = useQuery({ queryKey: ["tagintel", "catalog"], queryFn: api.tagintelCatalog });
  const [seeding, setSeeding] = useState(false);
  if (!hygieneQ.data?.available) return <NotLoaded />;
  const data = hygieneQ.data;
  const catalog = catalogQ.data?.entries ?? [];

  async function seed() {
    setSeeding(true);
    try { await api.tagintelCatalogSeed(sel, 12); await qc.invalidateQueries({ queryKey: ["tagintel", "catalog"] }); } finally { setSeeding(false); }
  }

  // Stage a remediation change-set built from hygiene findings, then jump to the Remediate tab
  // (which mounts fresh and reads the staged draft from its persisted state).
  function fix(name: string, description: string, ops: TagRemediationOp[]) {
    if (!ops.length) return;
    stageChangeSetToBuilder(name, description, ops);
    navigate("/tagintel/remediate");
  }
  const keyClusters = data.key_clusters ?? [];
  const valueClusters = data.value_clusters ?? [];
  const allKeyOps = keyClusters.flatMap(opsForKeyCluster);
  const allValueOps = valueClusters.flatMap(opsForValueCluster);

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        {/* Key clusters (F2) */}
        <div className="rounded-xl border bg-white">
          <div className="flex items-center justify-between border-b px-4 py-2">
            <span className="text-sm font-medium text-gray-700">Near-duplicate keys</span>
            <div className="flex items-center gap-2">
              {allKeyOps.length > 0 && (
                <button onClick={() => fix(`Normalize ${keyClusters.length} duplicate tag key(s)`, "Rename casing/separator variants to their canonical key (from Tag Hygiene).", allKeyOps)}
                  className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10" title="Build a change-set that renames every duplicate key variant to its canonical key">🔧 Fix all</button>
              )}
              <span className="text-[11px] text-gray-400">{keyClusters.length} clusters</span>
            </div>
          </div>
          <div className="max-h-72 space-y-2 overflow-auto p-3">
            {keyClusters.map((c: TagKeyCluster, i) => {
              const ops = opsForKeyCluster(c);
              return (
              <div key={i} className="rounded-lg border p-2">
                <div className="flex items-center gap-2">
                  <Chip kind={c.confidence} />
                  <span className="text-sm font-medium text-gray-800">{c.canonical}</span>
                  <span className="ml-auto text-[11px] text-gray-400">{c.affected} resources</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {c.members.map((m) => <span key={m} className={`rounded px-1.5 py-0.5 text-[11px] ${m === c.canonical ? "bg-emerald-100 text-emerald-700" : "bg-gray-100 text-gray-500 line-through"}`}>{m} ({c.counts[m]})</span>)}
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <p className="text-[11px] text-gray-400">{c.reason}</p>
                  {ops.length > 0 && (
                    <button onClick={() => fix(`Normalize key '${c.canonical}'`, `Rename ${ops.length} variant key(s) to '${c.canonical}' (from Tag Hygiene).`, ops)}
                      className="ml-auto rounded border px-2 py-0.5 text-[11px] text-brand hover:bg-brand/5" title={`Rename ${ops.map((o) => o.key).join(", ")} → ${c.canonical} in the Remediate tab`}>🔧 Fix</button>
                  )}
                </div>
              </div>
            ); })}
            {keyClusters.length === 0 && <p className="p-3 text-center text-xs text-gray-400">No duplicate keys — clean! ✨</p>}
          </div>
        </div>

        {/* Value clusters (F2) */}
        <div className="rounded-xl border bg-white">
          <div className="flex items-center justify-between border-b px-4 py-2">
            <span className="text-sm font-medium text-gray-700">Value variants</span>
            <div className="flex items-center gap-2">
              {allValueOps.length > 0 && (
                <button onClick={() => fix(`Normalize values for ${valueClusters.length} key(s)`, "Normalize value variants to their canonical value (from Tag Hygiene).", allValueOps)}
                  className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10" title="Build a change-set that normalizes every value variant to its canonical value">🔧 Fix all</button>
              )}
              <span className="text-[11px] text-gray-400">{valueClusters.length} keys</span>
            </div>
          </div>
          <div className="max-h-72 space-y-2 overflow-auto p-3">
            {valueClusters.map((c: TagValueCluster, i) => {
              const ops = opsForValueCluster(c);
              return (
              <div key={i} className="rounded-lg border p-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">{c.key}</span>
                  {ops.length > 0 && (
                    <button onClick={() => fix(`Normalize values for '${c.key}'`, `Normalize ${ops.length} value variant(s) on '${c.key}' (from Tag Hygiene).`, ops)}
                      className="ml-auto rounded border px-2 py-0.5 text-[11px] text-brand hover:bg-brand/5" title={`Normalize value variants on ${c.key} in the Remediate tab`}>🔧 Fix</button>
                  )}
                </div>
                {c.variants.map((v, j) => (
                  <div key={j} className="mt-1 flex flex-wrap items-center gap-1 text-[11px]">
                    {v.members.map((m) => <span key={m} className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-500">{m}</span>)}
                    <span className="text-gray-400">→</span>
                    <span className="rounded bg-emerald-100 px-1.5 py-0.5 font-medium text-emerald-700">{v.canonical}</span>
                    <span className="ml-auto text-gray-400">{v.affected}</span>
                  </div>
                ))}
              </div>
            ); })}
            {valueClusters.length === 0 && <p className="p-3 text-center text-xs text-gray-400">No value inconsistencies found.</p>}
          </div>
        </div>
      </div>

      {/* Canonical catalog (F2) */}
      <div className="rounded-xl border bg-white">
        <div className="flex items-center justify-between border-b px-4 py-2">
          <span className="text-sm font-medium text-gray-700">Canonical tag catalog</span>
          <button onClick={seed} disabled={seeding} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50">{seeding ? "Seeding…" : "✨ Seed from discovered keys"}</button>
        </div>
        {catalog.length === 0 ? (
          <p className="p-4 text-center text-xs text-gray-400">No catalog entries yet. Seed from your discovered keys to define a standard.</p>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400"><tr><th className="px-4 py-2">Canonical</th><th className="px-2">Category</th><th className="px-2">Aliases</th><th className="px-2">Required</th><th className="px-2">Scope</th><th /></tr></thead>
              <tbody>
                {catalog.map((e: TagCatalogEntry) => (
                  <tr key={e.id} className="border-t">
                    <td className="px-4 py-1.5 font-medium text-gray-800">{e.canonical}</td>
                    <td className="px-2"><span className="rounded px-1.5 py-0.5 text-[10px] text-white" style={{ background: CAT_COLORS[e.category] }}>{e.category}</span></td>
                    <td className="px-2 text-[11px] text-gray-500">{e.aliases.join(", ") || "—"}</td>
                    <td className="px-2">{e.required ? <span className="rounded bg-blue-100 px-1.5 text-[10px] text-blue-700">required</span> : <span className="text-gray-300">—</span>}</td>
                    <td className="px-2 text-[11px] text-gray-500">{e.scope}</td>
                    <td className="px-2 text-right"><button onClick={async () => { await api.tagintelCatalogDelete(e.id); void qc.invalidateQueries({ queryKey: ["tagintel", "catalog"] }); }} className="text-[11px] text-gray-400 hover:text-red-600">Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Workload grouping signals (F3) */}
      <div className="rounded-xl border bg-white">
        <div className="flex items-center justify-between border-b px-4 py-2">
          <span className="text-sm font-medium text-gray-700">Inferred workload grouping</span>
          {data.grouping && <span className="text-[11px] text-gray-400">{data.grouping.confirmed_resources} confirmed · {data.grouping.inferred_groups.length} groups</span>}
        </div>
        <div className="grid gap-2 p-3 sm:grid-cols-2 lg:grid-cols-3">
          {(data.grouping?.inferred_groups ?? []).slice(0, 18).map((g) => (
            <div key={g.id} className="rounded-lg border p-2">
              <div className="flex items-center gap-2"><Chip kind={g.confidence} /><span className="truncate text-sm font-medium text-gray-800">{g.label}</span></div>
              <div className="mt-1 text-[11px] text-gray-500">{g.signal}</div>
              <div className="mt-1 text-[11px] text-gray-400">{g.resource_count} resources · {g.subscription_count} sub(s)</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ============================================================ COVERAGE (F6)
function CoverageTab({ sel, scopeKey }: { sel: TagScopeSel; scopeKey: string }) {
  // `input` is what the user is typing; `applied` is what actually drives the query — so the
  // coverage check only runs when the user presses Check / Enter, not on every keystroke.
  const [input, setInput] = useState("");
  const [applied, setApplied] = useState("");
  const covQ = useQuery({ queryKey: ["tagintel", "coverage", scopeKey, applied], queryFn: () => api.tagintelCoverage(sel, applied || undefined) });
  const d: TagCoverageResponse | undefined = covQ.data;
  if (covQ.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (!d?.available) return <NotLoaded />;
  if (d.needs_required) {
    return (
      <div className="space-y-3">
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">{d.message}</div>
        <div className="rounded-xl border bg-white p-4">
          <div className="text-sm font-medium text-gray-700">Quick required-tag check</div>
          <p className="mt-1 text-xs text-gray-500">Enter required tag keys (comma-separated) and press Check, or define them in the Hygiene catalog.</p>
          <div className="mt-2 flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && input.trim()) setApplied(input.trim()); }}
              placeholder="CostCenter, Environment, Owner"
              className="flex-1 rounded-lg border px-3 py-2 text-sm"
            />
            <button
              onClick={() => setApplied(input.trim())}
              disabled={!input.trim()}
              className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50"
            >Check</button>
          </div>
        </div>
      </div>
    );
  }
  const gauge = [{ name: "coverage", value: d.coverage_pct ?? 0, fill: (d.coverage_pct ?? 0) >= 80 ? "#16a34a" : (d.coverage_pct ?? 0) >= 50 ? "#d97706" : "#dc2626" }];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-4">
        <div className="rounded-xl border bg-white p-3 text-center">
          <ResponsiveContainer width="100%" height={130}>
            <RadialBarChart innerRadius="70%" outerRadius="100%" data={gauge} startAngle={90} endAngle={-270}>
              <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
              <RadialBar dataKey="value" cornerRadius={8} background />
            </RadialBarChart>
          </ResponsiveContainer>
          <div className="-mt-20 mb-8 text-2xl font-semibold text-gray-900">{d.coverage_pct}%</div>
          <div className="mt-6 text-[11px] uppercase text-gray-400">Required-tag coverage</div>
        </div>
        <Kpi label="Evaluated" value={(d.evaluated ?? 0).toLocaleString()} sub={`${d.exempt ?? 0} exempt`} />
        <Kpi label="Compliant" value={(d.compliant ?? 0).toLocaleString()} tone="text-emerald-600" />
        <Kpi label="Missing one tag" value={(d.missing_one_total ?? 0).toLocaleString()} tone="text-amber-600" sub="highest-ROI fixes" />
      </div>

      {/* Per-key coverage bars */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium text-gray-700">Coverage by required key</div>
        <ResponsiveContainer width="100%" height={Math.max(120, (d.per_key?.length ?? 1) * 38)}>
          <BarChart layout="vertical" data={d.per_key} margin={{ left: 20, right: 40 }}>
            <XAxis type="number" domain={[0, 100]} tickFormatter={(v) => `${v}%`} fontSize={11} />
            <YAxis type="category" dataKey="key" width={120} fontSize={12} />
            <Tooltip formatter={(v) => `${v}%`} />
            <Bar dataKey="coverage_pct" radius={[0, 4, 4, 0]}>
              {(d.per_key ?? []).map((p, i) => <Cell key={i} fill={p.coverage_pct >= 80 ? "#16a34a" : p.coverage_pct >= 50 ? "#d97706" : "#dc2626"} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Missing-one queue */}
      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">"Missing only one tag" — fix queue</div>
        <div className="divide-y">
          {(d.missing_one ?? []).map((g) => (
            <div key={g.key} className="px-4 py-2">
              <div className="flex items-center gap-2">
                <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">+ {g.key}</span>
                <span className="text-sm text-gray-600">{g.count} resources need only this tag</span>
                <Link to="/tagintel/remediate" className="ml-auto text-[11px] text-brand hover:underline">Send to Remediate →</Link>
              </div>
              <div className="mt-1 flex flex-wrap gap-1">
                {g.resources.slice(0, 8).map((r) => <span key={r.id} className="truncate rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-500" title={r.id}>{r.name}</span>)}
                {g.count > 8 && <span className="text-[11px] text-gray-400">+{g.count - 8} more</span>}
              </div>
            </div>
          ))}
          {(d.missing_one ?? []).length === 0 && <p className="p-4 text-center text-xs text-gray-400">No resources are a single tag away from compliance.</p>}
        </div>
      </div>
    </div>
  );
}

// ============================================================ COST (F4 + F5)
function CostTab({ sel, scopeKey }: { sel: TagScopeSel; scopeKey: string }) {
  const [dimension, setDimension] = useState("workload");
  const costQ = useQuery({ queryKey: ["tagintel", "cost", scopeKey, dimension], queryFn: () => api.tagintelCost(sel, dimension) });
  const billQ = useQuery({ queryKey: ["tagintel", "billing", scopeKey], queryFn: () => api.tagintelBillingMap(sel) });
  // Real tag keys found at this scope — read from the census cache the panel already populated,
  // so the "Cost by" dropdown lists the customer's ACTUAL tags (not a hard-coded guess).
  const censusQ = useQuery({ queryKey: ["tagintel", "census", scopeKey], queryFn: () => api.tagintelCensus(sel, false) });
  const tagKeys = (censusQ.data?.census?.keys ?? []).map((k) => k.key);
  const d: TagCostResponse | undefined = costQ.data;
  if (costQ.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (!d?.available) return <NotLoaded />;
  if (!d.cost_available) {
    return <div className="m-4 rounded-xl border border-dashed bg-gray-50 p-6 text-center text-sm text-gray-600">
      No cost data cached for this scope. Load it once on <Link to="/inventory/cost" className="text-brand underline">Inventory → Cost</Link> (needs Cost Management Reader), then return here.
    </div>;
  }
  const cur = d.currency;
  const treemap = (d.breakdown ?? []).map((b, i) => ({ name: b.label, size: Math.max(1, b.cost), fill: PALETTE[i % PALETTE.length] }));
  const bill: TagBillingMapResponse | undefined = billQ.data;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label="Total (30d)" value={fmtMoney(d.total_cost, cur)} />
        <Kpi label="Allocatable" value={`${d.allocatable_pct}%`} tone="text-emerald-600" sub={fmtMoney(d.allocatable_cost, cur)} />
        <Kpi label="Unallocatable" value={fmtMoney(d.unallocatable_cost, cur)} tone="text-red-600" sub="missing billing tag" />
        <Kpi label="Untagged spend" value={fmtMoney(d.untagged_cost, cur)} tone="text-amber-600" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium text-gray-700">Cost by</span>
            <select value={dimension} onChange={(e) => setDimension(e.target.value)} className="rounded border px-2 py-1 text-xs">
              <optgroup label="Structural">
                <option value="workload">Workload</option>
                <option value="subscription">Subscription</option>
              </optgroup>
              {tagKeys.length > 0 && (
                <optgroup label="Tags found at this scope">
                  {tagKeys.map((k) => <option key={k} value={k}>{k}</option>)}
                </optgroup>
              )}
            </select>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <Treemap data={treemap} dataKey="size" stroke="#fff" content={<CostCell currency={cur} />} />
          </ResponsiveContainer>
        </div>

        <div className="rounded-xl border bg-white">
          <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">Top unallocatable resources</div>
          <div className="max-h-56 overflow-auto">
            <table className="w-full text-sm">
              <tbody>
                {(d.unallocatable_resources ?? []).slice(0, 30).map((r) => (
                  <tr key={r.id} className="border-t"><td className="px-4 py-1.5 text-gray-700"><span className="inline-flex items-center gap-1.5">{r.name}{r.id && <a href={`https://portal.azure.com/#@/resource${r.id}/overview`} target="_blank" rel="noopener noreferrer" title="Open in Azure Portal" className="text-gray-300 transition hover:text-brand">↗</a>}</span></td><td className="px-2 text-[11px] text-gray-400">{r.type.split("/").pop()}</td><td className="px-4 py-1.5 text-right tabular-nums text-red-600">{fmtMoney(r.cost, cur)}</td></tr>
                ))}
                {(d.unallocatable_resources ?? []).length === 0 && <tr><td className="p-4 text-center text-xs text-gray-400">All cost is allocatable. ✨</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Billing map (F4) */}
      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">Billing code → workload → owner</div>
        <div className="overflow-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400"><tr><th className="px-4 py-2">Billing code</th><th className="px-2 text-right">Cost</th><th className="px-2 text-right">Resources</th><th className="px-2">Workloads</th><th className="px-2">Owner coverage</th></tr></thead>
            <tbody>
              {(bill?.rows ?? []).slice(0, 30).map((r) => (
                <tr key={r.billing_code} className={`border-t ${r.unallocated ? "bg-red-50" : ""}`}>
                  <td className="px-4 py-1.5 font-medium text-gray-800">{r.unallocated ? "⚠ Unallocated" : r.billing_code}</td>
                  <td className="px-2 text-right tabular-nums">{fmtMoney(r.cost, cur)}</td>
                  <td className="px-2 text-right tabular-nums">{r.resource_count}</td>
                  <td className="px-2 text-[11px] text-gray-500">{r.workloads.map((w) => w.name).join(", ") || "—"}</td>
                  <td className="px-2 text-[11px] text-gray-500">{r.owner_coverage_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function CostCell(props: any) {
  const { x, y, width, height, name, size, currency } = props;
  if (width < 8 || height < 8) return null;
  return (
    <g>
      <rect x={x} y={y} width={width} height={height} style={{ fill: props.fill, stroke: "#fff", opacity: 0.88 }} />
      {width > 60 && height > 26 && <text x={x + 4} y={y + 15} fill="#fff" fontSize={11}>{name}</text>}
      {width > 60 && height > 40 && <text x={x + 4} y={y + 30} fill="#fff" fontSize={10} opacity={0.8}>{fmtMoney(size, currency)}</text>}
    </g>
  );
}

// ============================================================ DRIFT (F7)
function DriftTab({ sel, scopeKey }: { sel: TagScopeSel; scopeKey: string }) {
  const qc = useQueryClient();
  const snapsQ = useQuery({ queryKey: ["tagintel", "drift", scopeKey], queryFn: () => api.tagintelDrift(sel) });
  const snaps = snapsQ.data?.snapshots ?? [];
  const [base, setBase] = useState(""); const [head, setHead] = useState("");
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<"added" | "removed" | "values" | "resources" | null>(null);
  const diffQ = useQuery({ queryKey: ["tagintel", "drift-diff", scopeKey, base, head], queryFn: () => api.tagintelDriftDiff(sel, base, head), enabled: !!base && !!head && base !== head });
  const diff: TagDriftDiff | undefined = diffQ.data;

  useEffect(() => {
    if (snaps.length >= 2 && !base && !head) { setHead(snaps[0].id); setBase(snaps[1].id); }
  }, [snaps, base, head]);
  // Reset the open drill-down whenever the compared pair changes.
  useEffect(() => { setDetail(null); }, [base, head]);

  async function snapshot() {
    setBusy(true);
    try { await api.tagintelDriftSnapshot(sel); await qc.invalidateQueries({ queryKey: ["tagintel", "drift", scopeKey] }); } finally { setBusy(false); }
  }
  const series = [...snaps].reverse().map((s) => ({ name: new Date(s.taken_at).toLocaleDateString(), coverage: s.coverage_pct, keys: s.distinct_keys }));

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <button onClick={snapshot} disabled={busy} className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">{busy ? "Capturing…" : "📸 Capture snapshot"}</button>
        <span className="text-xs text-gray-500">{snaps.length} snapshot(s) stored</span>
      </div>

      {snaps.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-600">No snapshots yet. Capture one now, then again later to see drift.</div>
      ) : (
        <>
          <div className="rounded-xl border bg-white p-4">
            <div className="mb-2 text-sm font-medium text-gray-700">Coverage over time</div>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={series}>
                <defs><linearGradient id="cov" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#2563eb" stopOpacity={0.4} /><stop offset="100%" stopColor="#2563eb" stopOpacity={0} /></linearGradient></defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis dataKey="name" fontSize={11} /><YAxis domain={[0, 100]} fontSize={11} tickFormatter={(v) => `${v}%`} />
                <Tooltip /><Area type="monotone" dataKey="coverage" stroke="#2563eb" fill="url(#cov)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          <div className="rounded-xl border bg-white p-4">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-gray-700">Compare</span>
              <select value={base} onChange={(e) => setBase(e.target.value)} className="rounded border px-2 py-1 text-xs">{snaps.map((s) => <option key={s.id} value={s.id}>{new Date(s.taken_at).toLocaleString()}</option>)}</select>
              <span className="text-gray-400">→</span>
              <select value={head} onChange={(e) => setHead(e.target.value)} className="rounded border px-2 py-1 text-xs">{snaps.map((s) => <option key={s.id} value={s.id}>{new Date(s.taken_at).toLocaleString()}</option>)}</select>
            </div>
            {diff && !diff.error && (
              <>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                  <DriftKpi label="Coverage Δ" value={`${(diff.coverage_delta ?? 0) > 0 ? "+" : ""}${diff.coverage_delta}%`} tone={(diff.coverage_delta ?? 0) < 0 ? "text-red-600" : "text-emerald-600"} sub="vs base" />
                  <DriftKpi label="Keys added" value={diff.added_keys?.length ?? 0} sub={(diff.added_keys ?? []).slice(0, 3).join(", ") || "—"} active={detail === "added"} onClick={(diff.added_keys?.length ?? 0) ? () => setDetail(detail === "added" ? null : "added") : undefined} />
                  <DriftKpi label="Keys removed" value={diff.removed_keys?.length ?? 0} tone="text-amber-600" sub={(diff.removed_keys ?? []).slice(0, 3).join(", ") || "—"} active={detail === "removed"} onClick={(diff.removed_keys?.length ?? 0) ? () => setDetail(detail === "removed" ? null : "removed") : undefined} />
                  <DriftKpi label="Value changes" value={diff.value_change_count ?? 0} sub={`${diff.billing_changes?.length ?? 0} billing`} tone={(diff.billing_changes?.length ?? 0) > 0 ? "text-red-600" : "text-gray-900"} active={detail === "values"} onClick={(diff.value_change_count ?? 0) ? () => setDetail(detail === "values" ? null : "values") : undefined} />
                  <DriftKpi label="Resources changed" value={diff.changed_resource_count ?? 0} sub="click for details" active={detail === "resources"} onClick={(diff.changed_resource_count ?? 0) ? () => setDetail(detail === "resources" ? null : "resources") : undefined} />
                </div>
                {detail && <DriftDetail diff={diff} which={detail} onClose={() => setDetail(null)} />}
              </>
            )}
            {diff && (diff.billing_changes?.length ?? 0) > 0 && (
              <div className="mt-3 rounded-lg border border-red-200 bg-red-50 p-2">
                <div className="text-xs font-medium text-red-700">⚠ Billing-tag changes</div>
                {(diff.billing_changes ?? []).slice(0, 8).map((c, i) => <div key={i} className="text-[11px] text-red-600">{c.name || c.id.split("/").pop()} · {c.key}: {String(c.from)} → {String(c.to)}</div>)}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// A clickable KPI card for the Drift diff — highlights when its drill-down is open.
function DriftKpi({ label, value, sub, tone, active, onClick }: { label: string; value: React.ReactNode; sub?: string; tone?: string; active?: boolean; onClick?: () => void }) {
  const clickable = !!onClick;
  return (
    <button
      onClick={onClick}
      disabled={!clickable}
      className={`rounded-xl border p-3 text-left transition ${active ? "border-brand bg-brand/5" : "bg-white"} ${clickable ? "cursor-pointer hover:border-brand/50 hover:bg-brand/5" : "cursor-default"}`}
    >
      <div className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-400">{label}{clickable && <span className="text-brand">›</span>}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone || "text-gray-900"}`}>{value}</div>
      {sub && <div className="truncate text-[11px] text-gray-500">{sub}</div>}
    </button>
  );
}

// The drill-down panel that opens under the KPI cards — keys added/removed (with the resources
// affected), value changes, or every resource changed between the two snapshots.
function DriftDetail({ diff, which, onClose }: { diff: TagDriftDiff; which: "added" | "removed" | "values" | "resources"; onClose: () => void }) {
  const title = which === "added" ? "Keys added" : which === "removed" ? "Keys removed" : which === "values" ? "Value changes" : "Resources changed";
  return (
    <div className="mt-3 rounded-lg border bg-gray-50/60 p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-gray-700">{title}</span>
        <button onClick={onClose} className="ml-auto text-[11px] text-gray-400 hover:text-gray-700">✕ close</button>
      </div>

      {which === "added" && (
        <div className="space-y-2">
          {(diff.added_key_details ?? []).length === 0 ? <p className="text-xs text-gray-400">No keys were added.</p> :
            (diff.added_key_details ?? []).map((g) => (
              <div key={g.key} className="rounded border bg-white p-2">
                <div className="flex items-center gap-2"><span className="rounded bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700">+ {g.key}</span><span className="text-[11px] text-gray-500">added to {g.count} resource(s)</span></div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {g.resources.slice(0, 24).map((r) => <span key={r.id} title={r.id} className="truncate rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-600">{r.name || r.id.split("/").pop()}</span>)}
                  {g.count > 24 && <span className="text-[11px] text-gray-400">+{g.count - 24} more</span>}
                </div>
              </div>
            ))}
        </div>
      )}

      {which === "removed" && (
        <div className="space-y-2">
          {(diff.removed_key_details ?? []).length === 0 ? <p className="text-xs text-gray-400">No keys were removed.</p> :
            (diff.removed_key_details ?? []).map((g) => (
              <div key={g.key} className="rounded border bg-white p-2">
                <div className="flex items-center gap-2"><span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">− {g.key}</span><span className="text-[11px] text-gray-500">removed from {g.count} resource(s)</span></div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {g.resources.slice(0, 24).map((r) => <span key={r.id} title={r.id} className="truncate rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-600">{r.name || r.id.split("/").pop()}</span>)}
                  {g.count > 24 && <span className="text-[11px] text-gray-400">+{g.count - 24} more</span>}
                </div>
              </div>
            ))}
        </div>
      )}

      {which === "values" && (
        <div className="max-h-72 overflow-auto rounded border bg-white">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-gray-50 text-left text-[10px] uppercase text-gray-400"><tr><th className="px-2 py-1.5">Resource</th><th className="px-2">Key</th><th className="px-2">Before</th><th className="px-2">After</th></tr></thead>
            <tbody>
              {(diff.value_changes ?? []).length === 0 ? <tr><td className="px-2 py-2 text-gray-400" colSpan={4}>No values changed.</td></tr> :
                (diff.value_changes ?? []).map((c, i) => (
                  <tr key={i} className="border-t" title={c.id}>
                    <td className="px-2 py-1 text-gray-700">{c.name || c.id.split("/").pop()}</td>
                    <td className="px-2 text-gray-600">{c.key}</td>
                    <td className="px-2 font-mono text-[10px] text-red-600">{String(c.from)}</td>
                    <td className="px-2 font-mono text-[10px] text-emerald-700">{String(c.to)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}

      {which === "resources" && (
        <div className="space-y-2">
          {(diff.added_resources?.length || diff.removed_resources?.length) ? (
            <div className="flex flex-wrap gap-3 text-[11px]">
              {(diff.added_resources?.length ?? 0) > 0 && <span className="text-emerald-700">+{diff.added_resources?.length} resource(s) appeared</span>}
              {(diff.removed_resources?.length ?? 0) > 0 && <span className="text-amber-700">−{diff.removed_resources?.length} resource(s) disappeared</span>}
            </div>
          ) : null}
          <div className="max-h-72 space-y-1 overflow-auto">
            {(diff.changed_resources ?? []).length === 0 ? <p className="text-xs text-gray-400">No resources changed.</p> :
              (diff.changed_resources ?? []).map((r) => (
                <div key={r.id} className="rounded border bg-white p-2" title={r.id}>
                  <div className="text-sm font-medium text-gray-800">{r.name || r.id.split("/").pop()}</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {r.added.map((a, i) => <span key={`a${i}`} className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">+ {a.key}{a.to !== undefined ? `=${String(a.to)}` : ""}</span>)}
                    {r.removed.map((a, i) => <span key={`r${i}`} className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">− {a.key}</span>)}
                    {r.changed.map((a, i) => <span key={`c${i}`} className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-700">{a.key}: {String(a.from)} → {String(a.to)}</span>)}
                  </div>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}


// ============================================================ POLICY (F8)
function PolicyTab() {
  const ladderQ = useQuery({ queryKey: ["tagintel", "ladder"], queryFn: api.tagintelPolicyLadder });
  const catalogQ = useQuery({ queryKey: ["tagintel", "catalog"], queryFn: api.tagintelCatalog });
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [gen, setGen] = useState<Awaited<ReturnType<typeof api.tagintelPolicygen>> | null>(null);
  const [view, setView] = useState<TagPolicyDefinition | null>(null);
  const required = (catalogQ.data?.entries ?? []).filter((e) => e.required);
  const candidates = required.length ? required.map((e) => e.canonical) : ["CostCenter", "Environment", "Owner"];

  async function generate() {
    const sels = Object.entries(selections).filter(([, eff]) => eff).map(([tag, effect]) => ({ tag, effect, default_value: "" }));
    if (!sels.length) return;
    setGen(await api.tagintelPolicygen(sels));
  }

  // Hand the generated tag policies to the Rollout Planner (via sessionStorage, the same channel
  // the assessment report uses) so opening it lands with these definitions pre-loaded as context.
  function handoffToRollout(g: NonNullable<typeof gen>) {
    try {
      const handoff = {
        source: "tagintel" as const,
        definitions: g.definitions.map((d) => ({
          tag: d._tag,
          effect: d._effect,
          name: d.name,
          displayName: ((d.properties as Record<string, unknown>)?.displayName as string) || d.name,
          json: JSON.stringify(d.properties, null, 2),
        })),
      };
      sessionStorage.setItem("policyTagHandoff", JSON.stringify(handoff));
      // A tag hand-off and an assessment hand-off are mutually exclusive contexts; clear the other.
      sessionStorage.removeItem("policyHandoff");
    } catch { /* ignore storage failures */ }
  }

  return (
    <div className="space-y-4">
      {/* Rollout ladder */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-3 text-sm font-medium text-gray-700">Safe staged rollout</div>
        <div className="flex flex-wrap gap-2">
          {(ladderQ.data?.ladder ?? []).map((s) => (
            <div key={s.phase} className={`flex-1 rounded-lg border p-2 ${s.risk === "high" ? "border-red-200 bg-red-50" : s.risk === "medium" ? "border-amber-200 bg-amber-50" : "border-gray-200"}`}>
              <div className="text-[11px] font-medium text-gray-500">Phase {s.phase}</div>
              <div className="text-sm font-semibold text-gray-800">{s.name}</div>
              <div className="mt-0.5 text-[11px] text-gray-500">{s.description}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Generator */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-sm font-medium text-gray-700">Generate tag policies</div>
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase text-gray-400"><tr><th className="py-1">Tag</th><th>Effect</th></tr></thead>
          <tbody>
            {candidates.map((tag) => (
              <tr key={tag} className="border-t">
                <td className="py-2 font-medium text-gray-800">{tag}</td>
                <td>
                  <div className="flex gap-1">
                    {["", "audit", "inherit", "append", "deny"].map((eff) => (
                      <button key={eff} onClick={() => setSelections((s) => ({ ...s, [tag]: eff }))}
                        className={`rounded px-2 py-1 text-[11px] ${(selections[tag] || "") === eff ? (eff === "deny" ? "bg-red-600 text-white" : "bg-brand text-white") : "border text-gray-500 hover:bg-gray-50"}`}>
                        {eff || "none"}
                      </button>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button onClick={generate} className="mt-3 rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white">Generate definitions</button>
      </div>

      {gen && (
        <div className="rounded-xl border bg-white p-4">
          <div className="flex items-center justify-between"><span className="text-sm font-medium text-gray-700">{gen.definitions.length} definitions + initiative</span><Link to="/policy/rollout" onClick={() => handoffToRollout(gen)} className="text-[11px] text-brand hover:underline">Open Rollout Planner →</Link></div>
          {gen.warnings.map((w, i) => <div key={i} className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700">⚠ {w}</div>)}
          <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {gen.definitions.map((def) => (
              <button key={def.name} onClick={() => setView(def)} className="rounded-lg border p-2 text-left hover:bg-gray-50">
                <div className="flex items-center gap-1"><span className={`rounded px-1.5 py-0.5 text-[10px] text-white ${def._effect === "deny" ? "bg-red-600" : def._effect === "modify" ? "bg-amber-600" : "bg-blue-600"}`}>{def._effect}</span><span className="truncate text-xs font-medium text-gray-700">{def._tag}</span></div>
                <div className="mt-1 truncate text-[11px] text-gray-400">{def.name}</div>
              </button>
            ))}
          </div>
          {view && (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2"><span className="text-[11px] font-medium text-gray-500">{view.name}.json</span><CopyBtn text={JSON.stringify(view.properties, null, 2)} /></div>
              <pre className="max-h-72 overflow-auto rounded bg-gray-900 p-2 text-[11px] text-emerald-300">{JSON.stringify(view.properties, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================ REMEDIATE (F9 + F11)
const EMPTY_OP: TagRemediationOp = { type: "add_tag", key: "", value: "" };

// One row in the live apply status feed.
interface ApplyRow { index: number; name: string; change: string; status: "pending" | "ok" | "fail"; error?: string }

function opLabel(op: TagRemediationOp): string {
  if (op.type === "rename_key") return `rename ${op.key} → ${op.to_key}`;
  if (op.type === "normalize_value") return `${op.key}: ${op.from_value || "*"} → ${op.to_value}`;
  if (op.type === "remove_key") return `remove ${op.key}`;
  return `${op.type === "set_tag" ? "set" : "add"} ${op.key}=${op.value}`;
}

// A single op is "complete" only when every field it NEEDS is filled. This is the SAME rule the
// apply/preview path uses to keep an op (see `validOps` filters) — surfaced here so the editor can
// VISIBLY flag an incomplete op instead of silently dropping it. Returns "" when complete, else a
// short reason. (The classic trap this guards against: a "To key" left blank but looking filled
// because its placeholder text happened to read like a real value.)
function opIncompleteReason(op: TagRemediationOp): string {
  if (!op.key?.trim()) return op.type === "rename_key" ? "From key is required" : "Key is required";
  if (op.type === "rename_key" && !op.to_key?.trim()) return "To key is required";
  if (op.type === "normalize_value" && !op.to_value?.trim()) return "To value is required";
  if ((op.type === "add_tag" || op.type === "set_tag") && (op.value === undefined || op.value === "")) return "Value is required";
  return "";
}

// ---- Change-set library (advanced) -------------------------------------------------
const GROUP_COLORS: Record<string, { dot: string; chip: string; ring: string }> = {
  blue: { dot: "bg-blue-500", chip: "bg-blue-100 text-blue-700", ring: "border-blue-200" },
  green: { dot: "bg-emerald-500", chip: "bg-emerald-100 text-emerald-700", ring: "border-emerald-200" },
  amber: { dot: "bg-amber-500", chip: "bg-amber-100 text-amber-700", ring: "border-amber-200" },
  violet: { dot: "bg-violet-500", chip: "bg-violet-100 text-violet-700", ring: "border-violet-200" },
  rose: { dot: "bg-rose-500", chip: "bg-rose-100 text-rose-700", ring: "border-rose-200" },
  cyan: { dot: "bg-cyan-500", chip: "bg-cyan-100 text-cyan-700", ring: "border-cyan-200" },
  slate: { dot: "bg-slate-500", chip: "bg-slate-200 text-slate-700", ring: "border-slate-300" },
};
const GROUP_COLOR_KEYS = Object.keys(GROUP_COLORS);
const OP_TYPE_LABEL: Record<string, string> = { add_tag: "add", set_tag: "set", rename_key: "rename", normalize_value: "normalize", remove_key: "remove" };

function relTime(iso?: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// Trigger a client-side file download for in-memory text (export bundle).
function downloadText(filename: string, content: string, mime: string) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function ChangeSetLibrary({ changesets, groups, loadedId, onLoad, onNew, onChanged }: {
  changesets: TagChangeSet[];
  groups: TagChangeSetGroup[];
  loadedId: string;
  onLoad: (cs: TagChangeSet) => void;
  onNew: () => void;
  onChanged: () => void;
}) {
  const [search, setSearch] = useState("");
  const [groupFilter, setGroupFilter] = useState("");
  const [editing, setEditing] = useState<TagChangeSet | null>(null);
  const [groupForm, setGroupForm] = useState<Partial<TagChangeSetGroup> | null>(null);
  const [busyId, setBusyId] = useState("");
  const [ioBusy, setIoBusy] = useState<"" | "export" | "import">("");
  const [ioMsg, setIoMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const q = search.trim().toLowerCase();
  const filtered = changesets.filter((cs) => {
    if (groupFilter === "__ungrouped" ? !!cs.group_id : groupFilter && cs.group_id !== groupFilter) return false;
    if (!q) return true;
    return `${cs.name} ${cs.description} ${(cs.labels || []).join(" ")} ${(cs.affected_keys || []).join(" ")}`.toLowerCase().includes(q);
  });

  // Group the (filtered) sets: each defined group in order, then Ungrouped.
  const sections: { group: TagChangeSetGroup | null; sets: TagChangeSet[] }[] = [];
  for (const g of groups) {
    const sets = filtered.filter((c) => c.group_id === g.id);
    if (sets.length || (!q && !groupFilter)) sections.push({ group: g, sets });
  }
  const ungrouped = filtered.filter((c) => !c.group_id || !groups.some((g) => g.id === c.group_id));
  if (ungrouped.length || (!q && !groupFilter)) sections.push({ group: null, sets: ungrouped });

  async function act(id: string, fn: () => Promise<unknown>) {
    setBusyId(id);
    try { await fn(); onChanged(); } finally { setBusyId(""); }
  }

  // Export the whole library (or the current group filter) to a downloadable JSON file.
  async function exportLibrary() {
    setIoBusy("export"); setIoMsg(null);
    try {
      const ids = groupFilter
        ? filtered.map((c) => c.id)   // respect the active group filter when one is set
        : undefined;
      const bundle = await api.tagintelChangesetsExport(ids);
      const stamp = new Date().toISOString().slice(0, 10);
      downloadText(`tag-changesets-${stamp}.json`, JSON.stringify(bundle, null, 2), "application/json");
      setIoMsg({ ok: true, text: `Exported ${bundle.changesets.length} change-set(s).` });
    } catch (e) {
      setIoMsg({ ok: false, text: formatError(e) });
    } finally { setIoBusy(""); }
  }

  async function importFile(file: File) {
    setIoBusy("import"); setIoMsg(null);
    try {
      const bundle = JSON.parse(await file.text()) as TagChangeSetBundle;
      const r: TagChangeSetImportResult = await api.tagintelChangesetsImport(bundle);
      const bits = [`Imported ${r.imported} change-set(s)`];
      if (r.groups_created) bits.push(`${r.groups_created} new group(s)`);
      if (r.skipped) bits.push(`${r.skipped} skipped`);
      setIoMsg({ ok: r.imported > 0, text: bits.join(" · ") + (r.errors?.length ? ` — ${r.errors.slice(0, 3).join("; ")}` : "") });
      onChanged();
    } catch (e) {
      setIoMsg({ ok: false, text: e instanceof SyntaxError ? "That file isn't valid JSON." : formatError(e) });
    } finally { setIoBusy(""); }
  }

  return (
    <div className="rounded-xl border bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <span className="text-sm font-medium text-gray-700">📚 Change-set library</span>
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{changesets.length} set(s) · {groups.length} group(s)</span>
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, label, tag key…" className="ml-auto w-56 rounded border px-2 py-1 text-xs" />
        <select value={groupFilter} onChange={(e) => setGroupFilter(e.target.value)} className="rounded border px-2 py-1 text-xs">
          <option value="">All groups</option>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          <option value="__ungrouped">Ungrouped</option>
        </select>
        <input
          ref={fileRef} type="file" accept="application/json,.json" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void importFile(f); e.target.value = ""; }}
        />
        <button onClick={() => fileRef.current?.click()} disabled={ioBusy === "import"} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50" title="Import change-sets from a JSON file (added, never overwritten)">
          {ioBusy === "import" ? "Importing…" : "⤓ Import"}
        </button>
        <button onClick={() => void exportLibrary()} disabled={ioBusy === "export" || changesets.length === 0} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50" title={groupFilter ? "Export the filtered change-sets to a JSON file" : "Export all change-sets to a JSON file"}>
          {ioBusy === "export" ? "Exporting…" : "⤒ Export"}
        </button>
        <button onClick={() => setGroupForm({ name: "", color: GROUP_COLOR_KEYS[groups.length % GROUP_COLOR_KEYS.length] })} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">+ Group</button>
        <button onClick={onNew} className="rounded border border-brand/40 bg-brand/5 px-2 py-1 text-[11px] text-brand hover:bg-brand/10">+ New change-set</button>
      </div>

      {ioMsg && (
        <div className={`flex items-center gap-2 border-b px-4 py-1.5 text-[11px] ${ioMsg.ok ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
          <span>{ioMsg.ok ? "✓" : "⚠"} {ioMsg.text}</span>
          <button onClick={() => setIoMsg(null)} className="ml-auto text-gray-400 hover:text-gray-600">✕</button>
        </div>
      )}

      {groupForm && (
        <GroupEditor form={groupForm} onClose={() => setGroupForm(null)} onSaved={() => { setGroupForm(null); onChanged(); }} />
      )}

      {changesets.length === 0 ? (
        <p className="px-4 py-4 text-xs text-gray-400">No saved change-sets yet. Build one below and save it to start your library — then organize them into groups (e.g. “Ownership baseline”, “Cost allocation”, “Environment normalization”).</p>
      ) : (
        <div className="divide-y">
          {sections.map(({ group, sets }) => (
            <div key={group?.id ?? "__ungrouped"} className="px-3 py-2">
              <GroupHeader group={group} count={sets.length}
                onEdit={group ? () => setGroupForm(group) : undefined}
                onDelete={group ? () => act(group.id, () => api.tagintelChangesetGroupDelete(group.id)) : undefined} />
              {sets.length === 0 ? (
                <p className="px-2 py-1 text-[11px] text-gray-300">No change-sets in this group.</p>
              ) : (
                <div className="mt-1 grid gap-2 lg:grid-cols-2">
                  {sets.map((cs) => (
                    <ChangeSetCard key={cs.id} cs={cs} groups={groups} loaded={loadedId === cs.id} busy={busyId === cs.id}
                      onLoad={() => onLoad(cs)} onEdit={() => setEditing(cs)}
                      onDuplicate={() => act(cs.id, () => api.tagintelChangesetDuplicate(cs.id))}
                      onMove={(gid) => act(cs.id, () => api.tagintelChangesetMove(cs.id, gid))}
                      onDelete={() => act(cs.id, () => api.tagintelChangesetDelete(cs.id))} />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {editing && (
        <ChangeSetEditDrawer cs={editing} groups={groups} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); onChanged(); }} />
      )}
    </div>
  );
}

function GroupHeader({ group, count, onEdit, onDelete }: { group: TagChangeSetGroup | null; count: number; onEdit?: () => void; onDelete?: () => void }) {
  const c = group ? (GROUP_COLORS[group.color] ?? GROUP_COLORS.slate) : null;
  return (
    <div className="flex items-center gap-2 px-1">
      <span className={`inline-block h-2.5 w-2.5 rounded-full ${c?.dot ?? "bg-gray-300"}`} />
      <span className="text-xs font-semibold text-gray-700">{group?.name ?? "Ungrouped"}</span>
      <span className="text-[10px] text-gray-400">{count}</span>
      {group?.description && <span className="truncate text-[10px] text-gray-400">· {group.description}</span>}
      {onEdit && <button onClick={onEdit} className="ml-auto text-[10px] text-gray-400 hover:text-brand">edit</button>}
      {onDelete && <button onClick={onDelete} className="text-[10px] text-gray-400 hover:text-red-600">delete</button>}
    </div>
  );
}

function ChangeSetCard({ cs, groups, loaded, busy, onLoad, onEdit, onDuplicate, onMove, onDelete }: {
  cs: TagChangeSet; groups: TagChangeSetGroup[]; loaded: boolean; busy: boolean;
  onLoad: () => void; onEdit: () => void; onDuplicate: () => void; onMove: (gid: string) => void; onDelete: () => void;
}) {
  const [confirmDel, setConfirmDel] = useState(false);
  const breakdown = cs.op_breakdown ?? {};
  const lr = cs.last_run;
  return (
    <div className={`rounded-lg border p-2.5 ${loaded ? "border-brand/40 bg-brand/5" : "bg-white"} ${busy ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-sm font-medium text-gray-800">{cs.name}</span>
            {loaded && <span className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] text-brand">loaded</span>}
            {Object.entries(breakdown).map(([t, n]) => (
              <span key={t} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{n} {OP_TYPE_LABEL[t] ?? t}</span>
            ))}
          </div>
          {cs.description && <div className="truncate text-[11px] text-gray-500">{cs.description}</div>}
          <div className="truncate text-[11px] text-gray-400">{cs.operations.map(opLabel).join(" · ")}</div>
          {(cs.affected_keys?.length ?? 0) > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {(cs.affected_keys ?? []).slice(0, 6).map((k) => <span key={k} className="rounded bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-500">{k}</span>)}
            </div>
          )}
          {(cs.labels?.length ?? 0) > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {(cs.labels ?? []).map((l) => <span key={l} className="rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] text-violet-700">#{l}</span>)}
            </div>
          )}
          <div className="mt-1 text-[10px] text-gray-400">
            {lr ? <span className={lr.failed ? "text-amber-600" : "text-emerald-600"}>✓ applied {relTime(lr.at)} · {lr.applied}/{lr.total} on {lr.scope}{lr.failed ? ` · ${lr.failed} failed` : ""}</span>
                : <span>never run</span>}
            <span className="text-gray-300"> · updated {relTime(cs.updated_at)}</span>
          </div>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <button onClick={onLoad} className="rounded border px-2 py-1 text-[11px] text-brand hover:bg-brand/5">Load</button>
        <button onClick={onEdit} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">Edit</button>
        <button onClick={onDuplicate} className="rounded border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">Duplicate</button>
        <select value={cs.group_id || ""} onChange={(e) => onMove(e.target.value)} className="rounded border px-1.5 py-1 text-[11px] text-gray-600" title="Move to group">
          <option value="">Ungrouped</option>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
        {confirmDel ? (
          <span className="ml-auto flex items-center gap-1">
            <span className="text-[10px] text-red-600">Delete?</span>
            <button onClick={onDelete} className="rounded bg-red-600 px-2 py-1 text-[11px] text-white">Yes</button>
            <button onClick={() => setConfirmDel(false)} className="rounded border px-2 py-1 text-[11px] text-gray-500">No</button>
          </span>
        ) : (
          <button onClick={() => setConfirmDel(true)} className="ml-auto rounded border px-2 py-1 text-[11px] text-gray-400 hover:text-red-600">Delete</button>
        )}
      </div>
    </div>
  );
}

function GroupEditor({ form, onClose, onSaved }: { form: Partial<TagChangeSetGroup>; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(form.name ?? "");
  const [color, setColor] = useState(form.color ?? GROUP_COLOR_KEYS[0]);
  const [desc, setDesc] = useState(form.description ?? "");
  const [busy, setBusy] = useState(false);
  async function save() {
    if (!name.trim()) return;
    setBusy(true);
    try { await api.tagintelChangesetGroupSave({ id: form.id, name: name.trim(), color, description: desc }); onSaved(); }
    finally { setBusy(false); }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50 px-4 py-2">
      <span className="text-[11px] font-medium text-gray-600">{form.id ? "Edit group" : "New group"}</span>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Group name (e.g. Ownership baseline)" className="w-56 rounded border px-2 py-1 text-sm" />
      <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Description (optional)" className="w-48 rounded border px-2 py-1 text-xs" />
      <div className="flex items-center gap-1">
        {GROUP_COLOR_KEYS.map((k) => (
          <button key={k} onClick={() => setColor(k)} title={k} className={`h-5 w-5 rounded-full ${GROUP_COLORS[k].dot} ${color === k ? "ring-2 ring-offset-1 ring-gray-400" : ""}`} />
        ))}
      </div>
      <button onClick={save} disabled={busy || !name.trim()} className="rounded-lg bg-gray-900 px-3 py-1 text-xs text-white disabled:opacity-50">{busy ? "Saving…" : "Save group"}</button>
      <button onClick={onClose} className="rounded border px-2 py-1 text-xs text-gray-500">Cancel</button>
    </div>
  );
}

function ChangeSetEditDrawer({ cs, groups, onClose, onSaved }: { cs: TagChangeSet; groups: TagChangeSetGroup[]; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(cs.name);
  const [desc, setDesc] = useState(cs.description || "");
  const [groupId, setGroupId] = useState(cs.group_id || "");
  const [labels, setLabels] = useState<string[]>(cs.labels || []);
  const [labelInput, setLabelInput] = useState("");
  const [ops, setOps] = useState<TagRemediationOp[]>(cs.operations.length ? cs.operations.map((o) => ({ ...o })) : [{ ...EMPTY_OP }]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const validOps = ops.filter((o) => !opIncompleteReason(o));
  function patchOp(i: number, patch: Partial<TagRemediationOp>) { setOps(ops.map((o, idx) => (idx === i ? { ...o, ...patch } : o))); }
  function addLabel() { const l = labelInput.trim(); if (l && !labels.includes(l)) setLabels([...labels, l]); setLabelInput(""); }

  async function save() {
    if (!name.trim() || !validOps.length) { setErr("Name and at least one valid operation are required."); return; }
    setBusy(true); setErr("");
    try {
      await api.tagintelChangesetSave({ id: cs.id, name: name.trim(), description: desc, group_id: groupId, labels, operations: validOps });
      onSaved();
    } catch (e) { setErr(formatError(e)); } finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="h-full w-full max-w-2xl overflow-auto bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 flex items-center gap-2 border-b bg-white px-4 py-3">
          <span className="text-sm font-semibold text-gray-900">✏️ Edit change-set</span>
          <button onClick={onClose} className="ml-auto text-gray-400 hover:text-gray-700">✕</button>
        </div>
        <div className="space-y-3 p-4">
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-gray-500">Name<input value={name} onChange={(e) => setName(e.target.value)} className="mt-0.5 block w-full rounded border px-2 py-1 text-sm" /></label>
            <label className="text-xs text-gray-500">Group
              <select value={groupId} onChange={(e) => setGroupId(e.target.value)} className="mt-0.5 block w-full rounded border px-2 py-1 text-sm">
                <option value="">Ungrouped</option>
                {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            </label>
          </div>
          <label className="block text-xs text-gray-500">Description<input value={desc} onChange={(e) => setDesc(e.target.value)} className="mt-0.5 block w-full rounded border px-2 py-1 text-sm" /></label>
          <div>
            <div className="text-xs text-gray-500">Labels</div>
            <div className="mt-0.5 flex flex-wrap items-center gap-1">
              {labels.map((l) => (
                <span key={l} className="flex items-center gap-1 rounded-full bg-violet-100 px-2 py-0.5 text-[11px] text-violet-700">#{l}
                  <button onClick={() => setLabels(labels.filter((x) => x !== l))} className="text-violet-400 hover:text-violet-700">✕</button>
                </span>
              ))}
              <input value={labelInput} onChange={(e) => setLabelInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addLabel(); } }}
                placeholder="add label + Enter" className="w-32 rounded border px-2 py-1 text-[11px]" />
            </div>
          </div>

          <div>
            <div className="mb-1 flex items-center gap-2"><span className="text-xs font-medium text-gray-600">Operations</span><span className="text-[11px] text-gray-400">{validOps.length} valid</span>{ops.length > validOps.length && <span className="text-[11px] font-medium text-amber-600">· {ops.length - validOps.length} incomplete (skipped)</span>}</div>
            <div className="space-y-2">
              {ops.map((op, i) => {
                const incomplete = opIncompleteReason(op);
                return (
                <div key={i} className={`flex flex-wrap items-end gap-2 rounded-lg border bg-gray-50/50 p-2 ${incomplete ? "border-amber-300 bg-amber-50/40" : ""}`}>
                  <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">{i + 1}</span>
                  <label className="text-xs text-gray-500">Operation
                    <select value={op.type} onChange={(e) => patchOp(i, { type: e.target.value as TagRemediationOp["type"] })} className="mt-0.5 block rounded border px-2 py-1 text-sm">
                      <option value="add_tag">Add tag (if missing)</option>
                      <option value="set_tag">Set tag (overwrite)</option>
                      <option value="rename_key">Rename key</option>
                      <option value="normalize_value">Normalize value</option>
                      <option value="remove_key">Remove key (delete)</option>
                    </select>
                  </label>
                  <label className="text-xs text-gray-500">{op.type === "rename_key" ? "From key" : "Key"}<input value={op.key || ""} onChange={(e) => patchOp(i, { key: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. Owner" /></label>
                  {op.type === "rename_key" && <label className="text-xs text-gray-500">To key<input value={op.to_key || ""} onChange={(e) => patchOp(i, { to_key: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. owner" /></label>}
                  {(op.type === "add_tag" || op.type === "set_tag") && <label className="text-xs text-gray-500">Value<input value={op.value || ""} onChange={(e) => patchOp(i, { value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. team-a" /></label>}
                  {op.type === "normalize_value" && <><label className="text-xs text-gray-500">From value<input value={op.from_value || ""} onChange={(e) => patchOp(i, { from_value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. PRD" /></label><label className="text-xs text-gray-500">To value<input value={op.to_value || ""} onChange={(e) => patchOp(i, { to_value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. Production" /></label></>}
                  <button onClick={() => setOps(ops.length > 1 ? ops.filter((_, idx) => idx !== i) : ops)} disabled={ops.length === 1} className="rounded border px-2 py-1.5 text-xs text-gray-400 hover:text-red-600 disabled:opacity-30">✕</button>
                  {incomplete && <span className="w-full text-[11px] font-medium text-amber-600">⚠ {incomplete} — this operation will be skipped.</span>}
                </div>
                );
              })}
            </div>
            <button onClick={() => setOps([...ops, { ...EMPTY_OP }])} className="mt-2 rounded border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">+ Add key:value pair</button>
          </div>

          {/* Live before→after transformation preview (symbolic — the drawer has no dry-run plan). */}
          <ChangeSetFlow ops={ops} />

          {err && <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">{err}</div>}
          <div className="flex items-center gap-2">
            <button onClick={save} disabled={busy} className="rounded-lg bg-gray-900 px-4 py-1.5 text-sm text-white disabled:opacity-50">{busy ? "Saving…" : "💾 Save changes"}</button>
            <button onClick={onClose} className="rounded border px-3 py-1.5 text-sm text-gray-600">Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================ AI GENERATE
function GenerateTab({ sel, loaded }: { sel: TagScopeSel; loaded: boolean }) {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Awaited<ReturnType<typeof api.tagintelGenerate>> | null>(null);
  const [ops, setOps] = useState<TagGeneratedOp[]>([]);
  const [err, setErr] = useState("");

  const suggestions = [
    "Add environment=prod to everything missing it",
    "Set owner=platform-team on all storage accounts",
    "Tag every resource with cost-center=eng if it has none",
    "Normalize Environment values (PRD/Prod → Production)",
  ];

  async function generate(question: string) {
    if (!question.trim()) return;
    setBusy(true); setErr(""); setQ(question);
    try {
      const r = await api.tagintelGenerate({ question, ...sel });
      setRes(r);
      setOps((r.operations ?? []).map((o) => ({ ...o })));
    } catch (e) { setErr(formatError(e)); setRes(null); setOps([]); } finally { setBusy(false); }
  }

  function removeOp(i: number) { setOps(ops.filter((_, idx) => idx !== i)); }

  // Hand the reviewed proposal to the Remediate builder (same persisted-draft mechanism the
  // Hygiene "Fix in Remediate" handoff uses), then navigate there. The resolved resource_ids
  // ride along so the dry-run targets exactly what the AI proposed.
  function sendToRemediate() {
    if (!ops.length) return;
    const clean: TagRemediationOp[] = ops.map(({ rationale: _r, match_count: _m, ...op }) => op);
    stageChangeSetToBuilder("AI-generated tag set", res?.summary || q, clean);
    navigate("/tagintel/remediate");
  }

  if (!loaded) return <NotLoaded />;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border bg-gradient-to-br from-violet-50 to-white p-4">
        <div className="flex items-center gap-2">
          <span className="text-base">✨</span>
          <span className="text-sm font-medium text-gray-800">AI Tag Generator</span>
          <span className="text-[11px] text-gray-400">plain English → a grounded change-set</span>
        </div>
        <div className="mt-2 flex gap-2">
          <textarea
            value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && q.trim()) void generate(q); }}
            placeholder="e.g. Tag everything in this workload with environment=prod and add owner=platform-team to anything missing it"
            rows={2}
            className="flex-1 resize-y rounded-lg border px-3 py-2 text-sm"
          />
          <button onClick={() => q.trim() && generate(q)} disabled={busy} className="self-start rounded-lg bg-violet-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "…" : "✨ Propose tags"}</button>
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          {suggestions.map((s) => <button key={s} onClick={() => void generate(s)} className="rounded-full border bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">{s}</button>)}
        </div>
        <p className="mt-2 text-[11px] text-gray-400">The AI proposes only — nothing is written. Review the operations below, then send them to Remediate to dry-run, approve and apply.</p>
      </div>

      {err && <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}

      {busy && (
        <div className="flex items-center gap-2 rounded-lg border bg-white p-3 text-sm text-violet-700">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500" />
          Thinking… turning your instruction into grounded tag operations.
        </div>
      )}

      {res && !busy && (
        <div className="rounded-xl border bg-white">
          <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
            <span className="text-sm font-medium text-gray-700">Proposed change-set</span>
            {ops.length > 0 && <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-medium text-violet-700">{ops.length} operation(s)</span>}
            {res.summary && <span className="text-[11px] text-gray-500">{res.summary}</span>}
            <button onClick={sendToRemediate} disabled={!ops.length} className="ml-auto rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">Send to Remediate →</button>
          </div>

          {ops.length === 0 ? (
            <div className="p-6 text-center text-sm text-gray-500">No applicable operations were proposed for this scope.</div>
          ) : (
            <ul className="divide-y">
              {ops.map((op, i) => (
                <li key={i} className="flex items-start gap-3 px-4 py-2.5">
                  <span className="mt-0.5 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700">{OP_TYPE_LABEL[op.type] ?? op.type}</span>
                      <span className="font-mono text-xs text-gray-800">{opLabel(op)}</span>
                      {typeof op.match_count === "number" && (
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${op.match_count > 0 ? "bg-sky-100 text-sky-700" : "bg-gray-100 text-gray-400"}`}>{op.match_count} res</span>
                      )}
                    </div>
                    {op.rationale && <div className="mt-0.5 text-[11px] text-gray-500">{op.rationale}</div>}
                  </div>
                  <button onClick={() => removeOp(i)} className="rounded border px-2 py-1 text-xs text-gray-400 hover:text-red-600" title="Drop this operation">✕</button>
                </li>
              ))}
            </ul>
          )}

          {(res.notes ?? []).length > 0 && (
            <div className="border-t bg-amber-50/50 px-4 py-2">
              {(res.notes ?? []).map((n, i) => <div key={i} className="text-[11px] text-amber-700">⚠ {n}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RemediateTab({ sel, loaded, census, onRefreshScope, scanning }: { sel: TagScopeSel; loaded: boolean; census?: TagCensus; onRefreshScope: () => void; scanning: boolean }) {
  const qc = useQueryClient();
  // The working change-set (a list of key:value operations) + its name — persisted so a
  // half-built set survives navigating away, and can be preloaded from a saved set.
  const [csName, setCsName] = usePersistedState<string>(CS_PERSIST.name, "");
  const [csDesc, setCsDesc] = usePersistedState<string>(CS_PERSIST.desc, "");
  const [ops, setOps] = usePersistedState<TagRemediationOp[]>(CS_PERSIST.ops, [{ ...EMPTY_OP }]);
  const [loadedId, setLoadedId] = usePersistedState<string>(CS_PERSIST.loadedId, "");
  const [plan, setPlan] = useState<TagRemediationPlan | null>(null);
  const [scripts, setScripts] = useState<Awaited<ReturnType<typeof api.tagintelRemediateScriptsSet>> | null>(null);
  const [scriptTab, setScriptTab] = useState<"powershell" | "azcli" | "arg" | "rollback">("powershell");
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [approved, setApproved] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<TagApplyResult | null>(null);
  // Live apply status feed (drives the "show me everything it's doing" panel).
  const [applyStart, setApplyStart] = useState<TagApplyStart | null>(null);
  const [applyItems, setApplyItems] = useState<ApplyRow[]>([]);
  const [applyCounts, setApplyCounts] = useState<{ applied: number; failed: number; total: number }>({ applied: 0, failed: 0, total: 0 });
  const [appliedStale, setAppliedStale] = useState(false);   // post-apply: cached scope is now out of date
  // Two-way hover link between an editor row and its transformation lane in the flow preview.
  const [hoverOp, setHoverOp] = useState<number | null>(null);
  const setsQ = useQuery({ queryKey: ["tagintel", "changesets"], queryFn: api.tagintelChangesets });

  const validOps = ops.filter((o) => !opIncompleteReason(o));
  const canRun = validOps.length > 0;

  // Duplicate-key detection: how many ops share each (case-insensitive) key, so a row can flag
  // that the same key is targeted by more than one operation (common right after "Load current
  // tags", which brings every value of a key as its own row).
  const keyOccurrences = useMemo(() => {
    const m = new Map<string, number>();
    for (const o of ops) {
      const k = (o.key || "").trim().toLowerCase();
      if (k) m.set(k, (m.get(k) || 0) + 1);
    }
    return m;
  }, [ops]);

  // Census lookup: resources currently carrying a given key=value pair (case-insensitive key,
  // exact value), from the loaded census. Powers the "how many resources have this" hint.
  const censusCount = useMemo(() => {
    const byKey = new Map<string, Map<string, number>>();
    for (const k of census?.keys ?? []) {
      const vm = new Map<string, number>();
      for (const v of k.top_values ?? []) vm.set(v.value, v.count);
      byKey.set((k.key || "").toLowerCase(), vm);
    }
    return (key?: string, value?: string): number | null => {
      const vm = byKey.get((key || "").trim().toLowerCase());
      if (!vm) return null;
      return vm.get(value ?? "") ?? 0;
    };
  }, [census]);

  function patchOp(i: number, patch: Partial<TagRemediationOp>) {
    setOps(ops.map((o, idx) => (idx === i ? { ...o, ...patch } : o)));
  }
  function addOp() { setOps([...ops, { ...EMPTY_OP }]); }
  function removeOp(i: number) { if (ops.length > 1) setOps(ops.filter((_, idx) => idx !== i)); }

  // Build a prefill from the current estate: ONE `set_tag` op per (key, value) pair seen in the
  // census — bringing EVERYTHING, including duplicate keys with different values. The user then
  // edits / removes / dedups before running. Azure system + hidden tags are skipped. NOTE: the
  // census ships only each key's top values (server-cap), so very high-cardinality keys contribute
  // their most common values, not literally all of them.
  function buildPrefillOps(): TagRemediationOp[] {
    const isSystemKey = (k: string) => {
      const lk = k.toLowerCase();
      return lk.startsWith("hidden-") || lk.startsWith("link") || lk === "hidden-title" || lk === "ms-resource-usage";
    };
    const out: TagRemediationOp[] = [];
    for (const k of census?.keys ?? []) {
      if (!k.key || isSystemKey(k.key)) continue;
      const values = k.top_values?.length ? k.top_values : [{ value: "", count: 0 }];
      for (const v of values) {
        out.push({ type: "set_tag", key: k.key, value: v.value });
      }
    }
    return out;
  }
  const prefillCount = buildPrefillOps().length;

  function loadCurrentTags() {
    const prefill = buildPrefillOps();
    if (prefill.length === 0) return;
    // Replace-guard: only warn when the editor already holds meaningful (non-empty) work.
    const hasWork = ops.some((o) => o.key || o.value || o.to_key || o.from_value || o.to_value);
    if (hasWork && !confirm(`Replace the current ${ops.length} operation(s) with ${prefill.length} tag pair(s) from the estate? You can then remove or edit any before running.`)) return;
    setOps(prefill);
    setPlan(null); setScripts(null);
  }

  function loadSet(cs: TagChangeSet) {
    setCsName(cs.name); setCsDesc(cs.description || "");
    setOps(cs.operations.length ? cs.operations.map((o) => ({ ...o })) : [{ ...EMPTY_OP }]);
    setLoadedId(cs.id); setPlan(null); setScripts(null);
  }
  function newSet() { setCsName(""); setCsDesc(""); setOps([{ ...EMPTY_OP }]); setLoadedId(""); setPlan(null); setScripts(null); }

  // A scope refresh re-reads the estate, so any half-built change-set (and its dry-run preview)
  // is now against stale assumptions — clear the whole editor when a refresh STARTS so the user
  // rebuilds against the fresh census rather than silently applying old ops.
  const wasScanning = useRef(scanning);
  useEffect(() => {
    if (scanning && !wasScanning.current) newSet();
    wasScanning.current = scanning;
  }, [scanning]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSet() {
    if (!csName.trim() || !canRun) return;
    setSaving(true);
    try {
      // Preserve the loaded set's group + labels (the builder only edits name/desc/ops; the
      // library drawer owns grouping/labels) so an Update from here never wipes them.
      const prior = (setsQ.data?.changesets ?? []).find((c) => c.id === loadedId);
      const saved = await api.tagintelChangesetSave({
        id: loadedId || undefined, name: csName.trim(), description: csDesc,
        group_id: prior?.group_id || "", labels: prior?.labels || [], operations: validOps,
      });
      setLoadedId(saved.id);
      await qc.invalidateQueries({ queryKey: ["tagintel", "changesets"] });
    } finally { setSaving(false); }
  }
  async function preview() {
    setBusy(true); setScripts(null); setApproved(false); setConfirmRun(false); setApplyResult(null);
    setApplyItems([]); setApplyStart(null); setAppliedStale(false);
    try { setPlan(await api.tagintelRemediatePreviewSet(sel, validOps)); } catch { /* */ } finally { setBusy(false); }
  }
  async function genScripts() {
    setBusy(true);
    try { setScripts(await api.tagintelRemediateScriptsSet(sel, validOps)); } finally { setBusy(false); }
  }
  async function applyChanges() {
    setConfirmRun(false); setApplying(true); setApplyResult(null);
    setApplyItems([]); setApplyStart(null); setAppliedStale(false);
    setApplyCounts({ applied: 0, failed: 0, total: plan?.count ?? 0 });
    try {
      await streamTagintelRemediateApply(sel, validOps, loadedId || undefined, {
        onStart: (d) => { setApplyStart(d); setApplyCounts((c) => ({ ...c, total: d.total })); },
        onItemStart: (d) => setApplyItems((rows) => [...rows, { index: d.index, name: d.name, change: d.change, status: "pending" }]),
        onItemDone: (d) => {
          setApplyItems((rows) => rows.map((r) => (r.index === d.index ? { ...r, status: d.ok ? "ok" : "fail", error: d.error } : r)));
          setApplyCounts({ applied: d.applied, failed: d.failed, total: d.total });
        },
        onDone: (r) => {
          setApplyResult(r);
          if (!r.blocked && (r.applied ?? 0) > 0) setAppliedStale(true);
          void qc.invalidateQueries({ queryKey: ["tagintel"] });
          // Immediately refresh the Tag change history so the new recovery revision appears
          // the moment the apply finishes (the panel keys on ["tag-revisions", "tagintel"]).
          void qc.invalidateQueries({ queryKey: ["tag-revisions", "tagintel"] });
        },
        onError: (msg) => setApplyResult({ blocked: true, reason: msg }),
      });
    } catch (e) {
      setApplyResult({ blocked: true, reason: formatError(e) });
    } finally { setApplying(false); }
  }

  if (!loaded) return <NotLoaded />;
  const saved = setsQ.data?.changesets ?? [];

  return (
    <div className="space-y-4">
      {/* Tag change history — every applied change keeps a recovery copy and can be reverted. */}
      <TagRevisionsPanel mode="tagintel" />

      {/* Change-set library — grouped, searchable, editable (cloud-ops playbook shelf) */}
      <ChangeSetLibrary
        changesets={saved}
        groups={setsQ.data?.groups ?? []}
        loadedId={loadedId}
        onLoad={loadSet}
        onNew={newSet}
        onChanged={() => void qc.invalidateQueries({ queryKey: ["tagintel", "changesets"] })}
      />

      {/* Change-set builder (multiple key:value pairs) */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-gray-700">{loadedId ? "Edit change-set" : "Build a change-set"}</span>
          <span className="text-[11px] text-gray-400">(dry-run — no writes)</span>
          <input value={csName} onChange={(e) => setCsName(e.target.value)} placeholder="Change-set name (e.g. Baseline ownership tags)" className="ml-auto w-64 rounded border px-2 py-1 text-sm" />
          <input value={csDesc} onChange={(e) => setCsDesc(e.target.value)} placeholder="Description (optional)" className="w-56 rounded border px-2 py-1 text-sm" />
          <button onClick={saveSet} disabled={saving || !csName.trim() || !canRun} className="rounded-lg border border-brand/40 bg-brand/5 px-3 py-1.5 text-sm text-brand disabled:opacity-50">{saving ? "Saving…" : loadedId ? "💾 Update" : "💾 Save change-set"}</button>
        </div>

        <div className="space-y-2">
          {ops.map((op, i) => {
            const incomplete = opIncompleteReason(op);
            const dupCount = op.key ? (keyOccurrences.get(op.key.trim().toLowerCase()) || 0) : 0;
            const isDup = dupCount > 1;
            // Resource count from the census for this exact key=value (set/add ops only).
            const resCount = (op.type === "set_tag" || op.type === "add_tag") ? censusCount(op.key, op.value) : null;
            return (
            <div key={i} onMouseEnter={() => setHoverOp(i)} onMouseLeave={() => setHoverOp(null)} className={`flex flex-wrap items-end gap-2 rounded-lg border bg-gray-50/50 p-2 transition ${incomplete ? "border-amber-300 bg-amber-50/40" : isDup ? "border-orange-200" : ""} ${hoverOp === i ? "ring-2 ring-brand/30" : ""}`}>
              <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">{i + 1}</span>
              <label className="text-xs text-gray-500">Operation
                <select value={op.type} onChange={(e) => patchOp(i, { type: e.target.value as TagRemediationOp["type"] })} className="mt-0.5 block rounded border px-2 py-1 text-sm">
                  <option value="add_tag">Add tag (if missing)</option>
                  <option value="set_tag">Set tag (overwrite)</option>
                  <option value="rename_key">Rename key</option>
                  <option value="normalize_value">Normalize value</option>
                  <option value="remove_key">Remove key (delete)</option>
                </select>
              </label>
              <label className="text-xs text-gray-500">{op.type === "rename_key" ? "From key" : "Key"}<input value={op.key || ""} onChange={(e) => patchOp(i, { key: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. Owner" /></label>
              {op.type === "rename_key" && <label className="text-xs text-gray-500">To key<input value={op.to_key || ""} onChange={(e) => patchOp(i, { to_key: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. owner" /></label>}
              {(op.type === "add_tag" || op.type === "set_tag") && <label className="text-xs text-gray-500">Value<input value={op.value || ""} onChange={(e) => patchOp(i, { value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. team-a" /></label>}
              {op.type === "normalize_value" && <><label className="text-xs text-gray-500">From value<input value={op.from_value || ""} onChange={(e) => patchOp(i, { from_value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. PRD" /></label><label className="text-xs text-gray-500">To value<input value={op.to_value || ""} onChange={(e) => patchOp(i, { to_value: e.target.value })} className="mt-0.5 block rounded border px-2 py-1 text-sm" placeholder="e.g. Production" /></label></>}
              {/* Badges: duplicate-key + how many resources currently carry this key=value */}
              <div className="flex items-center gap-1.5 self-center">
                {isDup && <span className="rounded-full bg-orange-100 px-2 py-0.5 text-[10px] font-medium text-orange-700" title={`This key appears in ${dupCount} operations`}>⧉ dup ×{dupCount}</span>}
                {resCount !== null && <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${resCount > 0 ? "bg-sky-100 text-sky-700" : "bg-gray-100 text-gray-400"}`} title={resCount > 0 ? `${resCount} resource(s) currently have ${op.key}=${op.value}` : `No resource currently has ${op.key}=${op.value}`}>{resCount} res</span>}
              </div>
              <button onClick={() => removeOp(i)} disabled={ops.length === 1} className="self-center rounded border px-2 py-1.5 text-xs text-gray-400 hover:text-red-600 disabled:opacity-30" title="Remove pair">✕</button>
              {incomplete && <span className="w-full text-[11px] font-medium text-amber-600">⚠ {incomplete} — this operation will be skipped.</span>}
            </div>
            );
          })}
        </div>

        {/* Live before→after transformation preview (symbolic until a dry-run enriches it). */}
        <div className="mt-2">
          <ChangeSetFlow ops={ops} plan={plan} hoveredIndex={hoverOp} onHover={setHoverOp} />
        </div>

        <div className="mt-2 flex items-center gap-2">
          <button onClick={addOp} className="rounded border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">+ Add key:value pair</button>
          <button onClick={loadCurrentTags} disabled={!loaded || prefillCount === 0} title={!loaded ? "Load a scope first" : `Prefill ${prefillCount} key=value pair(s) from the current estate (duplicates included — remove/dedup before running)`} className="rounded border px-2 py-1 text-xs text-brand hover:bg-brand/5 disabled:opacity-40">⤓ Load current tags{prefillCount > 0 ? ` (${prefillCount})` : ""}</button>
          <span className="text-[11px] text-gray-400">{validOps.length} valid operation(s)</span>
          {ops.length > validOps.length && <span className="text-[11px] font-medium text-amber-600">· {ops.length - validOps.length} incomplete (skipped)</span>}
          <button onClick={preview} disabled={busy || !canRun} className="ml-auto rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">{busy ? "…" : "Preview (dry-run)"}</button>
        </div>
      </div>

      {plan && (
        <div className="rounded-xl border bg-white">
          <div className="flex flex-wrap items-center gap-3 border-b px-4 py-2">
            <span className="text-sm font-medium text-gray-700">Preview</span>
            <span className="rounded bg-gray-100 px-2 py-0.5 text-xs">{plan.count} resources</span>
            {(plan.overwrites ?? 0) > 0 && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-700">⚠ {plan.overwrites} overwrite value(s)</span>}
            <span className="text-[11px] text-gray-400">{plan.subscription_count} subscription(s)</span>
            {!plan.count && <span className="rounded bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500" title="Every targeted resource already matches the desired tags, so there is nothing to generate or apply.">Nothing to apply — already up to date</span>}
            <label className="ml-auto flex items-center gap-1 text-xs text-gray-600"><input type="checkbox" checked={approved} onChange={(e) => { setApproved(e.target.checked); setConfirmRun(false); }} /> I approve applying these tag changes</label>
            {/* Generate scripts is read-only review text — it does NOT require the approval checkbox
                (you generate scripts precisely to review BEFORE deciding to apply). Only a non-empty
                plan is required. */}
            <button onClick={genScripts} disabled={!plan.count} title={!plan.count ? "Nothing to script — the dry-run found 0 resources to change." : "Generate az CLI / PowerShell / rollback scripts to review or run manually"} className="rounded-lg border border-emerald-600 px-3 py-1.5 text-sm text-emerald-700 disabled:opacity-50">Generate scripts</button>
            <button onClick={() => setConfirmRun(true)} disabled={!approved || !plan.count || applying} className="rounded-lg bg-red-600 px-3 py-1.5 text-sm text-white disabled:opacity-50" title={!plan.count ? "Nothing to apply — the dry-run found 0 resources to change." : !approved ? "Check “I approve applying these tag changes” first." : "Run the tag changes against Azure now"}>
              {applying ? "Applying…" : "▶ Run on Azure"}
            </button>
          </div>
          {confirmRun && (
            <div className="flex flex-wrap items-center gap-2 border-b bg-red-50 px-4 py-2">
              <span className="text-xs text-red-700">⚠ This writes tags to <b>{plan.count}</b> live Azure resource(s){(plan.overwrites ?? 0) > 0 ? ` and overwrites ${plan.overwrites} existing value(s)` : ""}. This cannot be auto-undone (a rollback script is available under Generate scripts). Continue?</span>
              <div className="ml-auto flex gap-2">
                <button onClick={() => setConfirmRun(false)} className="rounded border px-2 py-1 text-xs text-gray-600 hover:bg-white">Cancel</button>
                <button onClick={applyChanges} className="rounded bg-red-600 px-3 py-1 text-xs text-white">Yes, apply to Azure</button>
              </div>
            </div>
          )}
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400"><tr><th className="px-4 py-2">Resource</th><th className="px-2">Before</th><th className="px-2">After</th></tr></thead>
              <tbody>
                {(plan.items ?? []).slice(0, 100).map((it) => (
                  <tr key={it.id} className="border-t align-top">
                    <td className="px-4 py-1.5 text-gray-700">{it.name}{it.overwrite && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-700">overwrite</span>}</td>
                    <td className="px-2 text-[11px] text-gray-400">{Object.entries(it.before).map(([k, v]) => `${k}=${v}`).join(", ") || "(none)"}</td>
                    <td className="px-2 text-[11px] text-emerald-700">{Object.entries(it.after).map(([k, v]) => `${k}=${v}`).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Live apply status — a detailed, per-resource feed of exactly what's being written. */}
      {(applying || applyItems.length > 0 || applyResult) && (
        <div className="rounded-xl border bg-white">
          <div className="flex flex-wrap items-center gap-3 border-b px-4 py-2">
            <span className="flex items-center gap-1.5 text-sm font-medium text-gray-700">
              {applying && <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-red-500" />}
              {applying ? "Applying tag changes…" : applyResult?.blocked ? "Apply blocked" : "Apply complete"}
            </span>
            {!applyResult?.blocked && <>
              <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700">{applyCounts.applied} applied</span>
              {applyCounts.failed > 0 && <span className="rounded bg-red-100 px-2 py-0.5 text-xs text-red-700">{applyCounts.failed} failed</span>}
              <span className="text-[11px] text-gray-400">of {applyCounts.total}{applyStart?.connection ? ` · ${applyStart.connection}` : ""} · live writes to Azure</span>
              {applyCounts.total > 0 && (
                <span className="ml-auto text-[11px] tabular-nums text-gray-400">{Math.round(((applyCounts.applied + applyCounts.failed) / Math.max(1, applyCounts.total)) * 100)}%</span>
              )}
            </>}
          </div>
          {applyResult?.blocked && (
            <div className="m-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">⚠ {applyResult.reason}</div>
          )}
          {!applyResult?.blocked && applyCounts.total > 0 && (
            <div className="h-1 w-full bg-gray-100">
              <div className="h-1 bg-red-500 transition-all duration-200" style={{ width: `${Math.round(((applyCounts.applied + applyCounts.failed) / Math.max(1, applyCounts.total)) * 100)}%` }} />
            </div>
          )}
          {applyItems.length > 0 && (
            <>
              <div className="max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <tbody>
                    {applyItems.slice(-200).map((r) => (
                      <tr key={r.index} className="border-t align-top">
                        <td className="w-7 px-2 py-1.5 text-center">
                          {r.status === "ok" ? <span className="text-emerald-600">✓</span>
                            : r.status === "fail" ? <span className="text-red-600">✕</span>
                            : <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600 align-middle" />}
                        </td>
                        <td className="px-2 py-1.5 text-gray-700">{r.name}</td>
                        <td className="px-2 py-1.5 text-[11px] text-gray-500">{r.change}</td>
                        {r.error && <td className="px-2 py-1.5 text-[11px] text-red-600">{r.error}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {applyItems.length > 200 && <div className="border-t px-4 py-1 text-[11px] text-gray-400">Showing the latest 200 of {applyItems.length} resources.</div>}
            </>
          )}
        </div>
      )}

      {/* Post-apply: the cached scope no longer reflects Azure — prompt a refresh. */}
      {appliedStale && (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-lg">⚠️</span>
            <div className="min-w-0">
              <div className="text-sm font-medium text-amber-900">Tag changes applied — this scope is now out of date</div>
              <div className="text-xs text-amber-700">{applyCounts.applied} resource(s) were re-tagged in Azure. Census, Hygiene, Coverage and the other tabs still show the pre-change scan. Refresh the scope to pull the new tags from Azure.</div>
            </div>
            <div className="ml-auto flex gap-2">
              <button onClick={() => { onRefreshScope(); setAppliedStale(false); }} disabled={scanning} className="rounded-lg bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:opacity-50">{scanning ? "Refreshing…" : "↻ Refresh scope now"}</button>
              <button onClick={() => setAppliedStale(false)} className="rounded-lg border border-amber-300 px-3 py-1.5 text-sm text-amber-700 hover:bg-amber-100">Dismiss</button>
            </div>
          </div>
        </div>
      )}

      {scripts?.scripts && (
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-gray-700">Generated scripts</span>
            {(["powershell", "azcli", "arg", "rollback"] as const).map((t) => (
              <button key={t} onClick={() => setScriptTab(t)} className={`rounded px-2 py-1 text-[11px] ${scriptTab === t ? "bg-brand text-white" : "border text-gray-500 hover:bg-gray-50"}`}>{t}</button>
            ))}
            <CopyBtn text={scripts.scripts[scriptTab]} />
          </div>
          <pre className="max-h-72 overflow-auto rounded bg-gray-900 p-3 text-[11px] text-emerald-300">{scripts.scripts[scriptTab]}</pre>
        </div>
      )}
    </div>
  );
}

