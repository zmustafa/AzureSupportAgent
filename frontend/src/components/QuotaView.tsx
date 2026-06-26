import { useMemo, useState, useRef, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import {
  api,
  type QuotaSnapshot,
  type QuotaResult,
  type QuotaMeta,
  type QuotaRun,
  type QuotaScanParams,
} from "../api";
import { usePersistedState } from "../utils/persistedState";
import { useDebounced, Skeleton } from "../utils/perf";
import { getQuotaScan, startQuotaScan, cancelQuotaScan, clearQuotaScan, useQuotaScanVersion } from "../utils/quotaScan";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { SubscriptionScopePicker } from "./SubscriptionScopePicker";

// ---------------------------------------------------------------- risk styling
const RISK_ORDER = ["Critical", "ThrottlingObserved", "Warning", "Watch", "Unknown", "Healthy"];
const RISK_LABEL: Record<string, string> = {
  Critical: "Critical",
  Warning: "Warning",
  Watch: "Watch",
  Healthy: "Healthy",
  Unknown: "Unknown",
  ThrottlingObserved: "Throttling",
};
const RISK_TEXT: Record<string, string> = {
  Critical: "text-red-700",
  Warning: "text-amber-700",
  Watch: "text-yellow-700",
  Healthy: "text-green-700",
  Unknown: "text-gray-500",
  ThrottlingObserved: "text-purple-700",
};
const RISK_BADGE: Record<string, string> = {
  Critical: "bg-red-100 text-red-700",
  Warning: "bg-amber-100 text-amber-700",
  Watch: "bg-yellow-100 text-yellow-700",
  Healthy: "bg-green-100 text-green-700",
  Unknown: "bg-gray-100 text-gray-600",
  ThrottlingObserved: "bg-purple-100 text-purple-700",
};
const RISK_ROW: Record<string, string> = {
  Critical: "bg-red-50",
  Warning: "bg-amber-50",
  Watch: "bg-yellow-50/60",
  ThrottlingObserved: "bg-purple-50",
  Healthy: "",
  Unknown: "",
};
const SOURCE_LABEL: Record<string, string> = {
  MicrosoftQuota: "Microsoft.Quota",
  ResourceProviderUsageApi: "Usage API",
  AzureResourceGraph: "Resource Graph",
  AzureMonitorMetric: "Monitor metric",
  StaticServiceLimit: "Static limit",
  ManualReviewRequired: "Manual review",
  NotSupported: "Not supported",
};

function pct(v: number | null): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(0)}%`;
}
function num(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
function agoText(seconds: number | null | undefined): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function fmtElapsed(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

// Sortable table columns. "" = default (risk rank → usage).
type SortKey = "" | "quota" | "sku_family" | "region" | "usage" | "limit" | "headroom" | "adjustable" | "source" | "risk";

function sortValue(r: QuotaResult, key: SortKey): string | number {
  switch (key) {
    case "quota": return `${r.service_name} ${r.quota_name}`.toLowerCase();
    case "sku_family": return (r.sku_family || "").toLowerCase();
    case "region": return (r.region || "").toLowerCase();
    case "usage": return r.percent_used ?? -1;
    case "limit": return r.limit ?? -1;
    case "headroom": return r.remaining ?? -1;
    case "adjustable": return (r.adjustable_status || "").toLowerCase();
    case "source": return (r.source_type || "").toLowerCase();
    case "risk": return RISK_ORDER.indexOf(r.risk_level);
    default: return 0;
  }
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

function UsageBar({ pctValue, risk }: { pctValue: number | null; risk: string }) {
  if (pctValue === null || pctValue === undefined) return <span className="text-gray-400">—</span>;
  const color =
    risk === "Critical" ? "bg-red-500"
      : risk === "Warning" ? "bg-amber-500"
        : risk === "Watch" ? "bg-yellow-400"
          : "bg-green-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-gray-200">
        <div className={`h-full ${color}`} style={{ width: `${Math.min(100, pctValue)}%` }} />
      </div>
      <span className="tabular-nums text-xs text-gray-700">{pctValue.toFixed(0)}%</span>
    </div>
  );
}

export function QuotaMonitorPanel() {
  const [connId, setConnId] = usePersistedState<string>("azsup.quota.connId", "");
  const [subId, setSubId] = usePersistedState<string>("azsup.quota.subId", "");
  const [subName, setSubName] = usePersistedState<string>("azsup.quota.subName", "");
  const [selRegions, setSelRegions] = usePersistedState<string[]>("azsup.quota.regions", []);
  const [selCats, setSelCats] = usePersistedState<string[]>("azsup.quota.categories", []);
  // Include zero-usage rows (e.g. every VM SKU family with headroom) — like the portal Quotas blade.
  const [showUnused, setShowUnused] = usePersistedState<boolean>("azsup.quota.showUnused", true);

  // QU3 — display filters are seeded FROM the URL so a shared link / refresh restores the view,
  // and reflected back into the URL by an effect below. (Read once at mount.)
  const [, setParams] = useSearchParams();
  const p0 = useRef(new URLSearchParams(window.location.search)).current;
  const [fRegion, setFRegion] = useState(p0.get("region") || "all");
  const [fProvider, setFProvider] = useState(p0.get("provider") || "all");
  const [fCategory, setFCategory] = useState(p0.get("category") || "all");
  const [fRisk, setFRisk] = useState(p0.get("risk") || "all");
  const [fKind, setFKind] = useState(p0.get("kind") || "all"); // dynamic | static | throttling | manual
  const [fFamilyOnly, setFFamilyOnly] = useState(p0.get("families") === "1");
  // Usage-range filter (percent of limit). [0,100] = no filter; rows with no usage% are kept
  // only when the range still covers the full 0–100 span.
  const [usageMin, setUsageMin] = useState(Number(p0.get("umin") ?? 0) || 0);
  const [usageMax, setUsageMax] = useState(Number(p0.get("umax") ?? 100) || 100);
  const [query, setQuery] = useState(p0.get("q") || "");
  const dQuery = useDebounced(query, 150); // QP2 — debounce so each keystroke doesn't re-filter+sort+rerender all rows
  // Table sort. Default "" = the risk-then-usage ranking; clicking a header overrides it. QU4 —
  // persisted per browser so the chosen sort survives reloads.
  const [sortKey, setSortKey] = usePersistedState<SortKey>("azsup.quota.sortKey", "");
  const [sortDir, setSortDir] = usePersistedState<"asc" | "desc">("azsup.quota.sortDir", "desc");
  const [drawer, setDrawer] = useState<QuotaResult | null>(null);
  const [showRegions, setShowRegions] = useState(false);
  const [regionQuery, setRegionQuery] = useState("");
  const [showCats, setShowCats] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [toast, setToast] = useState(""); // QU8 — export confirmation
  const [scanMinimized, setScanMinimized] = useState(false); // QP5 — minimise the progress popup

  const scopeKey = `sub:${subId || "none"}`;

  // QP5 — scan state lives in a module-level registry so it survives navigation. The component
  // re-renders on any registry change via useQuotaScanVersion().
  useQuotaScanVersion();
  const scan = getQuotaScan(scopeKey);
  const scanning = !!scan?.scanning;
  const [scanElapsed, setScanElapsed] = useState(0);
  const logEndRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const consumedDoneRef = useRef(0); // one-shot guard so a completed scan fires its "loaded" effect once

  // QU3 — reflect the active display filters back into the URL (shareable / back-button aware).
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    const setOrDel = (k: string, v: string, def: string) => { if (v && v !== def) next.set(k, v); else next.delete(k); };
    setOrDel("region", fRegion, "all"); setOrDel("provider", fProvider, "all");
    setOrDel("category", fCategory, "all"); setOrDel("risk", fRisk, "all"); setOrDel("kind", fKind, "all");
    if (fFamilyOnly) next.set("families", "1"); else next.delete("families");
    if (usageMin > 0) next.set("umin", String(usageMin)); else next.delete("umin");
    if (usageMax < 100) next.set("umax", String(usageMax)); else next.delete("umax");
    if (query.trim()) next.set("q", query.trim()); else next.delete("q");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fRegion, fProvider, fCategory, fRisk, fKind, fFamilyOnly, usageMin, usageMax, query]);

  // Nothing fetches on mount — the operator clicks "Load" to read the latest cached snapshot, or
  // "Run scan" to collect fresh. `loaded` gates the Azure-/server-hitting queries so opening the
  // page (or switching subscription) never auto-refreshes. A scan still lands its result via the
  // query cache (setQueryData), so results appear after a scan even without an explicit Load.
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setLoaded(false);
  }, [scopeKey, connId]);

  // Tick the elapsed-time counter while a scan is in flight (driven by the registry's startedAt).
  useEffect(() => {
    if (!scanning || !scan) return;
    const id = setInterval(() => setScanElapsed(Math.floor((Date.now() - scan.startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, [scanning, scan]);

  // Auto-scroll the activity log to the newest entry — WITHOUT moving the page. We scroll the
  // log's own container (scrollTop) instead of scrollIntoView, which would scroll every ancestor
  // (and yank the whole panel) when the snapshot below is tall.
  useEffect(() => {
    const el = logEndRef.current?.parentElement;
    if (el) el.scrollTop = el.scrollHeight;
  }, [scan?.log.length]);

  const metaQ = useQuery<QuotaMeta>({ queryKey: ["quotaMeta"], queryFn: api.quotaMeta, staleTime: 30 * 60 * 1000 });
  const regionsQ = useQuery({
    queryKey: ["quotaRegions", connId, subId],
    queryFn: () => api.quotaRegions(subId, connId),
    // Region list loads once the user opens the region picker or has loaded/scanned this scope —
    // not automatically on page load.
    enabled: !!subId && (loaded || showRegions),
    staleTime: 10 * 60 * 1000,
  });

  // Overview only READS the cache (server-side). Gated behind an explicit Load so the page never
  // auto-fetches on mount. A miss returns never_loaded.
  const overviewQ = useQuery<QuotaSnapshot>({
    queryKey: ["quota", connId, scopeKey],
    queryFn: () => api.quotaOverview(subId, connId, false),
    enabled: !!subId && loaded,
  });
  const data = overviewQ.data;
  const results = data?.results ?? [];

  // Keep the results body pinned to the top whenever a new snapshot loads (Load / scan / scope
  // change), so the table never appears pre-scrolled after the short empty-state is replaced.
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [scopeKey, connId, loaded, data?.generated_at]);

  const runsQ = useQuery<{ runs: QuotaRun[] }>({
    queryKey: ["quotaRuns", subId],
    queryFn: () => api.quotaRuns(subId, 20),
    enabled: !!subId && loaded,
    staleTime: 5 * 60 * 1000,
  });

  // Load the latest cached snapshot for the current scope on demand (no scan).
  function loadCached() {
    if (loaded) void overviewQ.refetch();
    else setLoaded(true);
  }

  // QP5 — kick the scan off in the module-level registry so it survives navigation. The popup is
  // just a live view of that registry; closing/minimising it doesn't stop the scan.
  function runScan() {
    if (scanning) return;
    setMsg(null);
    setScanMinimized(false);
    const params: QuotaScanParams = {
      subscription_id: subId, connection_id: connId, demo: false,
      regions: selRegions, categories: selCats, include_unused: showUnused,
    };
    startQuotaScan(scopeKey, params, ["quota", connId, scopeKey], {
      subLabel: subName || subId,
      regionLabel: selRegions.length ? `${selRegions.length} region(s)` : "all regions",
    });
  }

  function cancelScan() {
    cancelQuotaScan(scopeKey);
  }

  // QP5 — when a scan in the registry completes, mark the scope loaded + surface a result banner
  // exactly once (even if the user navigated away and back during the scan).
  useEffect(() => {
    if (!scan || scan.scanning || !scan.finishedAt || scan.finishedAt === consumedDoneRef.current) return;
    consumedDoneRef.current = scan.finishedAt;
    setLoaded(true);
    if (scan.error) setMsg({ text: scan.error, ok: false });
    else setMsg({ text: `Scanned ${scan.regionsScanned} region(s).`, ok: true });
    clearQuotaScan(scopeKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scan?.finishedAt, scan?.scanning]);

  const allRegions = regionsQ.data?.regions ?? [];
  const allCats = metaQ.data?.categories ?? [];

  // Group regions by Azure geography (US, Europe, Asia Pacific, …) — mirrors the docs region list —
  // and filter by the picker's search box.
  const regionGroups = useMemo(() => {
    const q = regionQuery.trim().toLowerCase();
    const groups: Record<string, typeof allRegions> = {};
    for (const r of allRegions) {
      if (q && !(`${r.display_name} ${r.name} ${r.geography} ${r.physical_location}`.toLowerCase().includes(q))) continue;
      const g = r.geography_group || "Other";
      (groups[g] ||= []).push(r);
    }
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [allRegions, regionQuery]);

  const providerOptions = useMemo(() => {
    const set = new Set<string>();
    results.forEach((r) => r.provider_namespace && set.add(r.provider_namespace));
    return Array.from(set).sort();
  }, [results]);
  const regionOptions = useMemo(() => {
    const set = new Set<string>();
    results.forEach((r) => set.add(r.region || "(subscription)"));
    return Array.from(set).sort();
  }, [results]);
  const categoryOptions = useMemo(() => {
    const set = new Set<string>();
    results.forEach((r) => r.quota_category && set.add(r.quota_category));
    return Array.from(set).sort();
  }, [results]);

  // QP4 — the (heavy) filter pass is memoised separately from the (light) sort pass, so toggling
  // sort direction or column doesn't re-run the whole filter over hundreds of rows. The search box
  // feeds the DEBOUNCED query (QP2) so keystrokes don't thrash the filter.
  const filteredUnsorted = useMemo(() => {
    const q = dQuery.trim().toLowerCase();
    return results.filter((r) => {
      if (fRegion !== "all" && (r.region || "(subscription)") !== fRegion) return false;
      if (fProvider !== "all" && r.provider_namespace !== fProvider) return false;
      if (fCategory !== "all" && r.quota_category !== fCategory) return false;
      if (fFamilyOnly && !r.sku_family) return false;
      if (fRisk !== "all" && r.risk_level !== fRisk) return false;
      // Usage-range filter. When the range is narrowed from the full 0–100 span, only rows that
      // report a usage% within [min,max] pass (rows without a usage% are excluded).
      if (usageMin > 0 || usageMax < 100) {
        const pu = r.percent_used;
        if (pu === null || pu === undefined) return false;
        if (pu < usageMin || pu > usageMax) return false;
      }
      if (fKind !== "all") {
        const kind =
          r.quota_category === "throttling" ? "throttling"
            : r.source_type === "StaticServiceLimit" ? "static"
              : r.source_type === "ManualReviewRequired" ? "manual"
                : "dynamic";
        if (kind !== fKind) return false;
      }
      if (q) {
        const hay = `${r.service_name} ${r.quota_name} ${r.sku_family} ${r.region} ${r.provider_namespace}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [results, fRegion, fProvider, fCategory, fFamilyOnly, fRisk, fKind, dQuery, usageMin, usageMax]);

  const filtered = useMemo(() => {
    const rows = [...filteredUnsorted];
    rows.sort((a, b) => {
      if (sortKey) {
        const va = sortValue(a, sortKey);
        const vb = sortValue(b, sortKey);
        let cmp: number;
        if (typeof va === "number" && typeof vb === "number") cmp = va - vb;
        else cmp = String(va).localeCompare(String(vb));
        if (cmp !== 0) return sortDir === "asc" ? cmp : -cmp;
        // Stable tiebreak by quota name.
        return a.quota_name.localeCompare(b.quota_name);
      }
      // Default ranking: most urgent risk first, then highest usage.
      const ra = RISK_ORDER.indexOf(a.risk_level);
      const rb = RISK_ORDER.indexOf(b.risk_level);
      if (ra !== rb) return ra - rb;
      return (b.percent_used ?? -1) - (a.percent_used ?? -1);
    });
    return rows;
  }, [filteredUnsorted, sortKey, sortDir]);

  // QP1 — virtualize the results table so only the visible rows are in the DOM (the live estate
  // reaches ~500-650 rows). A dedicated scroll container + spacer rows keep <table> semantics +
  // the sticky header + column alignment.
  const tableScrollRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => tableScrollRef.current,
    estimateSize: () => 49,
    overscan: 16,
  });
  const vItems = rowVirtualizer.getVirtualItems();
  const vPadTop = vItems.length ? vItems[0].start : 0;
  const vPadBottom = vItems.length ? rowVirtualizer.getTotalSize() - vItems[vItems.length - 1].end : 0;
  // Reset the table scroll to top whenever the filtered set changes shape materially.
  useEffect(() => { tableScrollRef.current?.scrollTo({ top: 0 }); }, [dQuery, fRegion, fProvider, fCategory, fRisk, fKind, data?.generated_at]);

  const counts = data?.counts ?? {};
  const unregistered = (data?.provider_registration ?? []).filter((p) => !p.registered);
  const neverLoaded = !!data?.never_loaded;
  const notConfigured = data && !data.connection_configured;
  const canScan = !!subId;

  // QU6 — active display filters as removable chips. (Scan-scope region/category pickers are
  // separate; these are the post-load table filters.)
  const activeFilters = useMemo(() => {
    const chips: { key: string; label: string; clear: () => void }[] = [];
    if (fRisk !== "all") chips.push({ key: "risk", label: `Risk: ${RISK_LABEL[fRisk] ?? fRisk}`, clear: () => setFRisk("all") });
    if (fKind !== "all") chips.push({ key: "kind", label: `Kind: ${fKind}`, clear: () => setFKind("all") });
    if (fRegion !== "all") chips.push({ key: "region", label: `Region: ${fRegion}`, clear: () => setFRegion("all") });
    if (fProvider !== "all") chips.push({ key: "provider", label: `Provider: ${fProvider}`, clear: () => setFProvider("all") });
    if (fCategory !== "all") chips.push({ key: "category", label: `Category: ${fCategory}`, clear: () => setFCategory("all") });
    if (fFamilyOnly) chips.push({ key: "fam", label: "VM families", clear: () => setFFamilyOnly(false) });
    if (usageMin > 0 || usageMax < 100) chips.push({ key: "usage", label: `Usage ${usageMin}–${usageMax}%`, clear: () => { setUsageMin(0); setUsageMax(100); } });
    if (query.trim()) chips.push({ key: "q", label: `“${query.trim()}”`, clear: () => setQuery("") });
    return chips;
  }, [fRisk, fKind, fRegion, fProvider, fCategory, fFamilyOnly, usageMin, usageMax, query]);
  function clearFilters() {
    setFRisk("all"); setFKind("all"); setFRegion("all"); setFProvider("all"); setFCategory("all");
    setFFamilyOnly(false); setUsageMin(0); setUsageMax(100); setQuery("");
  }

  // QU7 — flag a stale cache (older than its server TTL) so the header can nudge a re-scan.
  const cacheStale = !!(data && !neverLoaded && data.stale_cache);

  // QU2 — at-risk trend from the scan-history runs (oldest→newest for the chart) + a since-last
  // delta from the newest run's diff.
  const runs = runsQ.data?.runs ?? [];
  const trend = useMemo(() => {
    return [...runs].reverse().map((r) => ({
      at: r.started_at ? new Date(r.started_at).toLocaleDateString([], { month: "short", day: "numeric" }) : "",
      Critical: r.critical_count,
      Warning: r.warning_count,
      Watch: r.watch_count ?? 0,
    }));
  }, [runs]);
  const lastDiff = runs[0]?.diff ?? null;

  // QU8 — export the CURRENT filtered view to CSV client-side (the server export is the full
  // snapshot). Mirrors the column set shown in the table.
  function exportFiltered() {
    const headers = ["quota_name", "service_name", "provider", "category", "sku_family", "region", "usage", "limit", "remaining", "percent_used", "unit", "adjustable", "source", "risk", "recommendation"];
    const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = [headers.join(",")];
    for (const r of filtered) {
      lines.push([
        esc(r.quota_name), esc(r.service_name), esc(r.provider_namespace), esc(r.quota_category),
        esc(r.sku_family), esc(r.region), esc(r.current_usage ?? ""), esc(r.limit ?? ""), esc(r.remaining ?? ""),
        esc(r.percent_used ?? ""), esc(r.unit), esc(r.adjustable_status), esc(r.source_type), esc(r.risk_level),
        esc(r.recommendation),
      ].join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `quota-${subName || subId}-${new Date().toISOString().slice(0, 10)}.csv`; a.click();
    URL.revokeObjectURL(url);
    setToast(`Exported ${filtered.length.toLocaleString()} row${filtered.length === 1 ? "" : "s"} to CSV`);
  }

  // QU8 — auto-dismiss the export toast.
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(""), 2800); return () => clearTimeout(t); }, [toast]);

  function toggleRegion(name: string) {
    setSelRegions(selRegions.includes(name) ? selRegions.filter((r) => r !== name) : [...selRegions, name]);
  }
  function toggleCat(name: string) {
    setSelCats(selCats.includes(name) ? selCats.filter((c) => c !== name) : [...selCats, name]);
  }
  // Click a column header to sort by it; click again to flip direction; numeric columns start
  // descending (highest first), text columns ascending.
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir(["usage", "limit", "headroom", "risk"].includes(key) ? "desc" : "asc");
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-col gap-2">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              📊 Quota Monitor
            </h1>
            <p className="text-xs text-gray-500">
              Proactive Azure quota posture by subscription &amp; region — usage, limits, headroom,
              risk, and whether each limit is adjustable, hard, or needs manual review.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ConnectionScopePicker value={connId} onChange={(id) => setConnId(id)} />
            <SubscriptionScopePicker
              value={subId}
              valueName={subName}
              onPick={(id, name) => { setSubId(id); setSubName(name); }}
              connectionId={connId}
            />
            {/* Region multi-select */}
            <div className="relative">
                <button
                  onClick={() => { setShowRegions((v) => !v); setShowCats(false); }}
                  disabled={!subId}
                  className="rounded-lg border bg-white px-2.5 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                  🌍 {selRegions.length ? `${selRegions.length} region(s)` : "All regions"} ▾
                </button>
                {showRegions && (
                  <div className="absolute right-0 z-50 mt-1 flex max-h-96 w-72 flex-col rounded-md border bg-white shadow-lg">
                    <div className="flex items-center justify-between gap-2 border-b px-2 py-1.5">
                      <button className="text-[11px] text-brand hover:underline" onClick={() => setSelRegions([])}>All</button>
                      <span className="text-[11px] text-gray-400">
                        {allRegions.length} region(s){selRegions.length ? ` · ${selRegions.length} selected` : ""}
                      </span>
                      <button className="text-[11px] text-gray-400 hover:underline" onClick={() => setShowRegions(false)}>Close</button>
                    </div>
                    <input
                      value={regionQuery}
                      onChange={(e) => setRegionQuery(e.target.value)}
                      placeholder="Search region / geography…"
                      className="m-1.5 rounded border px-2 py-1 text-xs"
                    />
                    <div className="min-h-0 flex-1 overflow-auto px-1.5 pb-1.5">
                      {regionsQ.isLoading && <div className="px-2 py-1 text-xs text-gray-400">Loading regions…</div>}
                      {!regionsQ.isLoading && regionGroups.length === 0 && (
                        <div className="px-2 py-1 text-xs text-gray-400">No regions match.</div>
                      )}
                      {regionGroups.map(([group, regs]) => (
                        <div key={group} className="mb-1">
                          <div className="flex items-center justify-between px-1 pt-1.5 pb-0.5">
                            <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">{group}</span>
                            <button
                              className="text-[10px] text-brand hover:underline"
                              onClick={() => {
                                const names = regs.map((r) => r.name);
                                const allSel = names.every((n) => selRegions.includes(n));
                                setSelRegions(allSel
                                  ? selRegions.filter((n) => !names.includes(n))
                                  : Array.from(new Set([...selRegions, ...names])));
                              }}
                            >
                              {regs.every((r) => selRegions.includes(r.name)) ? "Clear" : "All"}
                            </button>
                          </div>
                          {regs.map((r) => (
                            <label key={r.name} className="flex items-center gap-2 rounded px-2 py-1 text-xs hover:bg-gray-50">
                              <input type="checkbox" checked={selRegions.includes(r.name)} onChange={() => toggleRegion(r.name)} />
                              <span className="min-w-0 flex-1 truncate" title={`${r.name}${r.physical_location ? ` · ${r.physical_location}` : ""}${r.paired_region ? ` · paired: ${r.paired_region}` : ""}`}>
                                {r.display_name}
                                {r.physical_location && <span className="ml-1 text-[10px] text-gray-400">{r.physical_location}</span>}
                              </span>
                              {r.has_availability_zones && (
                                <span className="shrink-0 rounded bg-emerald-50 px-1 py-0.5 text-[9px] font-medium text-emerald-600" title="Availability zones">AZ</span>
                              )}
                              {r.category === "Recommended" && (
                                <span className="shrink-0 rounded bg-blue-50 px-1 py-0.5 text-[9px] font-medium text-blue-600" title="Recommended region">★</span>
                              )}
                            </label>
                          ))}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
            </div>
            {/* Category multi-select */}
            <div className="relative">
              <button
                onClick={() => { setShowCats((v) => !v); setShowRegions(false); }}
                className="rounded-lg border bg-white px-2.5 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
              >
                🗂️ {selCats.length ? `${selCats.length} categor${selCats.length === 1 ? "y" : "ies"}` : "All categories"} ▾
              </button>
              {showCats && (
                <div className="absolute right-0 z-50 mt-1 max-h-72 w-52 overflow-auto rounded-md border bg-white p-1.5 shadow-lg">
                  <div className="flex items-center justify-between px-1 pb-1">
                    <button className="text-[11px] text-brand hover:underline" onClick={() => setSelCats([])}>All</button>
                    <button className="text-[11px] text-gray-400 hover:underline" onClick={() => setShowCats(false)}>Close</button>
                  </div>
                  {allCats.map((c) => (
                    <label key={c} className="flex items-center gap-2 rounded px-2 py-1 text-xs capitalize hover:bg-gray-50">
                      <input type="checkbox" checked={selCats.includes(c)} onChange={() => toggleCat(c)} />
                      <span>{c}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
            <label
              className="flex items-center gap-1 rounded-lg border bg-white px-2.5 py-1.5 text-xs text-gray-600"
              title="Include zero-usage rows so every VM SKU family / quota shows its limit and headroom (like the portal Quotas blade)."
            >
              <input type="checkbox" checked={showUnused} onChange={(e) => setShowUnused(e.target.checked)} />
              Show unused
            </label>
            <button
              onClick={() => loadCached()}
              disabled={scanning || !canScan || overviewQ.isFetching}
              title="Load the latest cached scan for this scope (no new Azure calls)"
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {overviewQ.isFetching ? "Loading…" : loaded ? "↻ Reload" : "⬇ Load"}
            </button>
            <button
              onClick={() => runScan()}
              disabled={scanning || !canScan}
              className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50"
            >
              {scanning ? "Scanning…" : "↻ Run scan"}
            </button>
            {/* QU7 — nudge a re-scan when the cached snapshot is older than its server TTL. */}
            {cacheStale && !scanning && (
              <button
                onClick={() => runScan()}
                title="This cached scan is older than its refresh interval — run a fresh scan."
                className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100"
              >
                ⚠ stale · rescan
              </button>
            )}
            {subId ? (
              <div className="flex items-center gap-1">
                <button onClick={exportFiltered} disabled={!data || filtered.length === 0} title="Export the current filtered view to CSV" className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50">⬇ Export view</button>
                <a href={api.quotaExportUrl(subId, false, "csv")} title="Full cached snapshot (server)" className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50">CSV</a>
                <a href={api.quotaExportUrl(subId, false, "json")} title="Full cached snapshot (server)" className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50">JSON</a>
              </div>
            ) : null}
          </div>
        </div>
        {msg && (
          <div className={`mt-2 rounded-md px-3 py-1.5 text-xs ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>
            {msg.text}
          </div>
        )}
      </div>

      {/* Body */}
      <div ref={bodyRef} className="min-h-0 flex-1 overflow-auto px-5 py-4" style={{ overflowAnchor: "none" }}>
        {notConfigured ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            No Azure connection is configured. Add one in Settings → Connections, then run a scan.
          </div>
        ) : !canScan ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            Select a subscription to begin, then click <b>Run scan</b>.
          </div>
        ) : !data && overviewQ.isFetching ? (
          <div className="space-y-3"><div className="grid grid-cols-3 gap-2 sm:grid-cols-6">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-14 animate-pulse rounded-lg bg-gray-100" />)}</div><Skeleton rows={10} /></div>
        ) : !data ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            Nothing loaded yet. Click <b>Load</b> to fetch the latest cached scan for
            {" "}<b>{subName || subId}</b>, or <b>Run scan</b> to collect fresh.
          </div>
        ) : neverLoaded ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            No scan yet for <b>{subName || subId}</b>. Click <b>Run scan</b> to collect quota across
            {selRegions.length ? ` ${selRegions.length} selected region(s)` : " all regions"}.
          </div>
        ) : overviewQ.isLoading ? (
          <Skeleton rows={12} />
        ) : (
          <>
            {/* KPI cards */}
            <div className="mb-3 grid grid-cols-3 gap-2 sm:grid-cols-6">
              <Stat label="Critical" value={counts.Critical ?? 0} tone="text-red-700" />
              <Stat label="Warning" value={counts.Warning ?? 0} tone="text-amber-700" />
              <Stat label="Watch" value={counts.Watch ?? 0} tone="text-yellow-700" />
              <Stat label="Throttling" value={counts.ThrottlingObserved ?? 0} tone="text-purple-700" />
              <Stat label="Unknown / static" value={counts.Unknown ?? 0} tone="text-gray-600" />
              <Stat label="Healthy" value={counts.Healthy ?? 0} tone="text-green-700" />
            </div>

            {/* Meta strip */}
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-500">
              <span>Subscription: <b className="text-gray-700">{data?.subscription_name || subId}</b></span>
              <span>Regions scanned: <b className="text-gray-700">{data?.regions_scanned?.length ?? 0}</b></span>
              <span>Last scan: <b className="text-gray-700">{agoText(data?.age_seconds)}</b></span>
              {data?.status && <span>Status: <b className="text-gray-700 capitalize">{data.status}</b></span>}
            </div>

            {/* Provider registration banner */}
            {unregistered.length > 0 && (
              <div className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <b>{unregistered.length} resource provider(s) not registered:</b>{" "}
                {unregistered.map((p) => p.namespace).join(", ")}. Some quotas may be unavailable until you register them.
              </div>
            )}

            {/* Capacity disclaimer */}
            <div className="mb-3 rounded-md border border-blue-100 bg-blue-50 px-3 py-1.5 text-[11px] text-blue-700">
              ℹ️ {data?.capacity_note}
            </div>

            {/* Filters */}
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <select value={fRisk} onChange={(e) => setFRisk(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
                <option value="all">All risk</option>
                {RISK_ORDER.map((r) => <option key={r} value={r}>{RISK_LABEL[r]}</option>)}
              </select>
              <select value={fKind} onChange={(e) => setFKind(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
                <option value="all">All limit kinds</option>
                <option value="dynamic">Live usage</option>
                <option value="static">Static limit</option>
                <option value="throttling">Throttling</option>
                <option value="manual">Manual review</option>
              </select>
              <select value={fRegion} onChange={(e) => setFRegion(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
                <option value="all">All regions</option>
                {regionOptions.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
              <select value={fProvider} onChange={(e) => setFProvider(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
                <option value="all">All providers</option>
                {providerOptions.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <select value={fCategory} onChange={(e) => setFCategory(e.target.value)} className="rounded-md border px-2 py-1 text-xs capitalize">
                <option value="all">All categories</option>
                {categoryOptions.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <label className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-gray-600" title="Show only VM SKU-family vCPU quotas">
                <input type="checkbox" checked={fFamilyOnly} onChange={(e) => setFFamilyOnly(e.target.checked)} />
                VM families
              </label>
              <UsageRangeSlider min={usageMin} max={usageMax} onChange={(lo, hi) => { setUsageMin(lo); setUsageMax(hi); }} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search quota / SKU…"
                className="min-w-[160px] flex-1 rounded-md border px-2 py-1 text-xs"
              />
              <span className="text-[11px] text-gray-500">
                showing <b className="text-gray-700">{filtered.length.toLocaleString()}</b> of {results.length.toLocaleString()}
                {activeFilters.length > 0 ? " (filtered)" : ""}
              </span>
            </div>

            {/* QU6 — active filter chips, each removable, with a clear-all. */}
            {activeFilters.length > 0 && (
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                {activeFilters.map((c) => (
                  <span key={c.key} className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
                    {c.label}
                    <button onClick={c.clear} className="text-brand/60 hover:text-brand">✕</button>
                  </span>
                ))}
                <button onClick={clearFilters} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear all</button>
              </div>
            )}

            {/* Table (virtualized) */}
            <div ref={tableScrollRef} className="max-h-[60vh] overflow-auto rounded-lg border bg-white">
              <table className="w-full text-sm">
                <thead className="sticky top-0 z-10 bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500 shadow-sm">
                  <tr>
                    <Th k="quota" label="Quota" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                    <Th k="sku_family" label="SKU family" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                    <Th k="region" label="Region" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                    <Th k="usage" label="Usage" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} num />
                    <Th k="limit" label="Limit" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} num />
                    <Th k="headroom" label="Headroom" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} num />
                    <Th k="adjustable" label="Adjustable" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                    <Th k="source" label="Source" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                    <Th k="risk" label="Risk" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  </tr>
                </thead>
                <tbody>
                  {vPadTop > 0 && <tr style={{ height: vPadTop }}><td colSpan={9} className="p-0" /></tr>}
                  {vItems.map((vi) => {
                    const r = filtered[vi.index];
                    return (
                    <tr
                      key={`${r.region}|${r.provider_namespace}|${r.quota_name}|${r.sku_family}|${vi.index}`}
                      ref={rowVirtualizer.measureElement}
                      data-index={vi.index}
                      onClick={() => setDrawer(r)}
                      className={`cursor-pointer border-t hover:bg-gray-50 ${RISK_ROW[r.risk_level] ?? ""}`}
                    >
                      <td className="px-3 py-2">
                        <div className="font-medium text-gray-900">{r.quota_name}</div>
                        <div className="text-[11px] text-gray-500">{r.service_name}</div>
                      </td>
                      <td className="px-3 py-2 text-xs text-gray-600">
                        {r.sku_family
                          ? <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-[10px] text-indigo-700">{r.sku_family}</span>
                          : <span className="text-gray-300">—</span>}
                      </td>
                      <td className="px-3 py-2 text-xs text-gray-600">{r.region || "—"}</td>
                      <td className="px-3 py-2"><UsageBar pctValue={r.percent_used} risk={r.risk_level} /></td>
                      <td className="px-3 py-2 text-xs tabular-nums text-gray-700">
                        {r.limit === null ? "—" : `${num(r.limit)} ${r.unit}`}
                      </td>
                      <td className="px-3 py-2 text-xs tabular-nums text-gray-700">{num(r.remaining)}</td>
                      <td className="px-3 py-2 text-xs text-gray-600">{r.adjustable_status}</td>
                      <td className="px-3 py-2 text-[11px] text-gray-500">{SOURCE_LABEL[r.source_type] ?? r.source_type}</td>
                      <td className="px-3 py-2">
                        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${RISK_BADGE[r.risk_level] ?? "bg-gray-100 text-gray-600"}`}>
                          {RISK_LABEL[r.risk_level] ?? r.risk_level}
                        </span>
                      </td>
                    </tr>
                    );
                  })}
                  {vPadBottom > 0 && <tr style={{ height: vPadBottom }}><td colSpan={9} className="p-0" /></tr>}
                  {filtered.length === 0 && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-sm text-gray-400">No quota rows match the filters.</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            {/* History trend (QU2) */}
            {trend.length > 1 && (
              <div className="mt-3 rounded-lg border bg-white p-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">At-risk trend</span>
                  {lastDiff && (lastDiff.new_at_risk.length > 0 || lastDiff.recovered.length > 0) && (
                    <span className="text-[11px] text-gray-500">
                      since last scan:{" "}
                      {lastDiff.new_at_risk.length > 0 && <b className="text-red-600">+{lastDiff.new_at_risk.length} at risk</b>}
                      {lastDiff.new_at_risk.length > 0 && lastDiff.recovered.length > 0 && " · "}
                      {lastDiff.recovered.length > 0 && <b className="text-green-600">−{lastDiff.recovered.length} recovered</b>}
                    </span>
                  )}
                  <span className="ml-auto text-[11px] text-gray-400">{trend.length} scans</span>
                </div>
                <ResponsiveContainer width="100%" height={140}>
                  <AreaChart data={trend} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                    <XAxis dataKey="at" fontSize={10} tickLine={false} />
                    <YAxis fontSize={10} allowDecimals={false} width={28} />
                    <Tooltip contentStyle={{ fontSize: 11 }} />
                    <Area type="monotone" dataKey="Critical" stackId="1" stroke="#dc2626" fill="#fecaca" />
                    <Area type="monotone" dataKey="Warning" stackId="1" stroke="#d97706" fill="#fed7aa" />
                    <Area type="monotone" dataKey="Watch" stackId="1" stroke="#ca8a04" fill="#fef08a" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </>
        )}
      </div>

      {/* QP5 — scan progress popup (minimisable). The scan runs in a module-level registry, so
          closing/minimising this overlay does NOT stop it; a header chip lets you reopen it. */}
      {scanning && !scanMinimized && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-lg overflow-hidden rounded-xl border bg-white shadow-2xl">
            <div className="flex items-center gap-2 border-b bg-violet-50/70 px-4 py-3">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-violet-300 border-t-violet-600" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-violet-900">Scanning quota…</div>
                <div className="truncate text-xs text-violet-600">{scan?.status || "Working…"}</div>
              </div>
              <span className="shrink-0 tabular-nums text-xs text-violet-500">{fmtElapsed(scanElapsed)}</span>
              <button onClick={() => setScanMinimized(true)} title="Run in background" className="shrink-0 rounded border px-2 py-0.5 text-[11px] text-violet-600 hover:bg-violet-50">— Minimise</button>
            </div>
            <div className="max-h-72 overflow-y-auto bg-white px-4 py-2 font-mono text-[11px] leading-relaxed text-gray-600">
              {(scan?.log ?? []).map((l, i) => (
                <div key={i} className="flex gap-2">
                  <span className="shrink-0 text-gray-400">{l.t}</span>
                  <span className="min-w-0 flex-1 break-words">{l.msg}</span>
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
            <div className="flex items-center justify-between border-t bg-gray-50 px-4 py-2">
              <span className="truncate text-[11px] text-gray-400">
                {(subName || subId) + " · " + (selRegions.length ? `${selRegions.length} region(s)` : "all regions")}
              </span>
              <button
                onClick={cancelScan}
                className="rounded-lg border border-red-200 bg-red-50 px-3 py-1 text-xs font-medium text-red-600 hover:bg-red-100"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* QP5 — minimised scan chip: the scan keeps running in the background; click to reopen. */}
      {scanning && scanMinimized && (
        <button
          onClick={() => setScanMinimized(false)}
          className="fixed bottom-5 right-5 z-50 flex items-center gap-2 rounded-full border border-violet-300 bg-white px-3 py-2 text-xs font-medium text-violet-700 shadow-lg hover:bg-violet-50"
        >
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-violet-300 border-t-violet-600" />
          Scanning… {fmtElapsed(scanElapsed)} <span className="text-violet-400">· open</span>
        </button>
      )}

      {/* QU8 — export confirmation toast. */}
      {toast && (
        <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 -translate-x-1/2 rounded-lg bg-gray-900/90 px-4 py-2 text-sm font-medium text-white shadow-lg">
          ✓ {toast}
        </div>
      )}

      {/* Detail drawer */}
      {drawer && (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/20" onClick={() => setDrawer(null)}>
          <div className="h-full w-full max-w-md overflow-auto bg-white p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-start justify-between">
              <div>
                <h2 className="text-base font-semibold text-gray-900">{drawer.quota_name}</h2>
                <p className="text-xs text-gray-500">{drawer.service_name} · {drawer.provider_namespace}</p>
              </div>
              <button onClick={() => setDrawer(null)} className="text-gray-400 hover:text-gray-700">✕</button>
            </div>
            <div className="space-y-2 text-sm">
              <Row k="Risk"><span className={`font-medium ${RISK_TEXT[drawer.risk_level]}`}>{RISK_LABEL[drawer.risk_level] ?? drawer.risk_level}</span></Row>
              <Row k="Region">{drawer.region || "(subscription-wide)"}</Row>
              {drawer.sku_family && <Row k="SKU family">{drawer.sku_family}</Row>}
              <Row k="Usage">{drawer.current_usage === null ? "—" : `${num(drawer.current_usage)} ${drawer.unit}`}</Row>
              <Row k="Limit">{drawer.limit === null ? "—" : `${num(drawer.limit)} ${drawer.unit}`}</Row>
              <Row k="Remaining">{num(drawer.remaining)}</Row>
              <Row k="Used">{pct(drawer.percent_used)}</Row>
              <Row k="Adjustable">{drawer.adjustable_status}</Row>
              <Row k="Source">{SOURCE_LABEL[drawer.source_type] ?? drawer.source_type}</Row>
              <Row k="Collection">{drawer.collection_status}</Row>
              <Row k="Checked">{drawer.last_checked_utc ? new Date(drawer.last_checked_utc).toLocaleString() : "—"}</Row>
            </div>
            {drawer.error_message && (
              <div className="mt-3 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{drawer.error_message}</div>
            )}
            <div className="mt-3 rounded-md bg-gray-50 px-3 py-2 text-xs text-gray-700">
              <div className="mb-1 font-medium text-gray-800">Recommendation</div>
              {drawer.recommendation}
            </div>

            {/* QU5 — actionable next steps. Read-only: the app never changes a quota; it links to
                the portal Quotas blade and copies an inspect/increase command to run yourself. */}
            <QuotaRowActions r={drawer} onToast={setToast} />

            {drawer.raw_provider_response != null && (
              <details className="mt-3">
                <summary className="cursor-pointer text-xs font-medium text-gray-600">Raw provider response</summary>
                <pre className="mt-1 max-h-72 overflow-auto rounded bg-gray-900 p-2 text-[10px] leading-relaxed text-gray-100">
                  {JSON.stringify(drawer.raw_provider_response, null, 2)}
                </pre>
              </details>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// QU5 — per-row cleanup/escalation actions for a quota: open the portal Quotas blade, and copy a
// read-only `az quota show` command (and a `--scope` you can adapt for an increase). The app never
// mutates a quota itself.
function QuotaRowActions({ r, onToast }: { r: QuotaResult; onToast: (m: string) => void }) {
  const portalUrl = "https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas";
  const scope = r.region
    ? `/subscriptions/${r.subscription_id}/providers/${r.provider_namespace}/locations/${r.region}`
    : `/subscriptions/${r.subscription_id}`;
  const cmd = `az quota show --resource-name "${r.quota_name}" --scope "${scope}"`;
  const copy = (text: string, label: string) => { void navigator.clipboard?.writeText(text); onToast(label); };
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <a href={portalUrl} target="_blank" rel="noopener noreferrer" className="rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-medium text-blue-700 hover:bg-blue-100">
        ↗ Request increase (Portal)
      </a>
      <button onClick={() => copy(cmd, "Copied az quota command")} title={cmd} className="rounded-lg border px-2.5 py-1 text-[11px] text-gray-600 hover:bg-gray-50">
        ⧉ Copy az quota show
      </button>
      <button onClick={() => copy(scope, "Copied quota scope")} title={scope} className="rounded-lg border px-2.5 py-1 text-[11px] text-gray-600 hover:bg-gray-50">
        ⧉ Copy scope
      </button>
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 border-b border-gray-100 pb-1">
      <span className="text-gray-500">{k}</span>
      <span className="text-right text-gray-800">{children}</span>
    </div>
  );
}

// Sortable column header. Shows ▲/▼ on the active column; click toggles direction.
function Th({
  k, label, sortKey, sortDir, onSort, num,
}: {
  k: SortKey;
  label: string;
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (k: SortKey) => void;
  num?: boolean;
}) {
  const active = sortKey === k;
  return (
    <th className="px-3 py-2">
      <button
        onClick={() => onSort(k)}
        className={`flex items-center gap-1 uppercase tracking-wide hover:text-gray-900 ${active ? "text-gray-900" : ""} ${num ? "" : ""}`}
        title={`Sort by ${label}`}
      >
        <span>{label}</span>
        <span className="text-[9px] text-gray-400">{active ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
      </button>
    </th>
  );
}

// Compact dual-thumb usage-range slider for the toolbar. Two overlaid range inputs (min/max) with
// a highlighted band between them; the active range is filtered on percent_used. 0–100 = no filter.
function UsageRangeSlider({ min, max, onChange }: { min: number; max: number; onChange: (lo: number, hi: number) => void }) {
  const active = min > 0 || max < 100;
  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-2 py-1 ${active ? "border-brand/40 bg-brand/5" : ""}`}
      title="Filter rows by usage % of limit"
    >
      <span className="text-[11px] text-gray-500">Usage</span>
      <div className="relative h-4 w-28">
        {/* track */}
        <div className="pointer-events-none absolute top-1/2 h-1 w-full -translate-y-1/2 rounded-full bg-gray-200" />
        {/* selected band */}
        <div
          className="pointer-events-none absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-brand"
          style={{ left: `${min}%`, right: `${100 - max}%` }}
        />
        <input
          type="range" min={0} max={100} step={1} value={min}
          onChange={(e) => onChange(Math.min(Number(e.target.value), max), max)}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent"
        />
        <input
          type="range" min={0} max={100} step={1} value={max}
          onChange={(e) => onChange(min, Math.max(Number(e.target.value), min))}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent"
        />
      </div>
      <span className="w-16 tabular-nums text-[11px] text-gray-700">{min}–{max}%</span>
      {active && (
        <button
          onClick={() => onChange(0, 100)}
          className="text-[11px] text-gray-400 hover:text-gray-700"
          title="Reset usage range"
        >
          ✕
        </button>
      )}
    </div>
  );
}

