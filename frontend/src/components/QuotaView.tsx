import { useMemo, useState, useRef, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  streamQuotaScan,
  type QuotaSnapshot,
  type QuotaResult,
  type QuotaMeta,
  type QuotaRun,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
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
  const qc = useQueryClient();
  const [connId, setConnId] = usePersistedState<string>("azsup.quota.connId", "");
  const [subId, setSubId] = usePersistedState<string>("azsup.quota.subId", "");
  const [subName, setSubName] = usePersistedState<string>("azsup.quota.subName", "");
  const [selRegions, setSelRegions] = usePersistedState<string[]>("azsup.quota.regions", []);
  const [selCats, setSelCats] = usePersistedState<string[]>("azsup.quota.categories", []);
  // Include zero-usage rows (e.g. every VM SKU family with headroom) — like the portal Quotas blade.
  const [showUnused, setShowUnused] = usePersistedState<boolean>("azsup.quota.showUnused", true);

  // Filters
  const [fRegion, setFRegion] = useState("all");
  const [fProvider, setFProvider] = useState("all");
  const [fCategory, setFCategory] = useState("all");
  const [fRisk, setFRisk] = useState("all");
  const [fKind, setFKind] = useState("all"); // dynamic | static | throttling | manual
  const [fFamilyOnly, setFFamilyOnly] = useState(false);
  // Usage-range filter (percent of limit). [0,100] = no filter; rows with no usage% are kept
  // only when the range still covers the full 0–100 span.
  const [usageMin, setUsageMin] = useState(0);
  const [usageMax, setUsageMax] = useState(100);
  const [query, setQuery] = useState("");
  // Table sort. Default "" = the risk-then-usage ranking; clicking a header overrides it.
  const [sortKey, setSortKey] = useState<SortKey>("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [drawer, setDrawer] = useState<QuotaResult | null>(null);
  const [showRegions, setShowRegions] = useState(false);
  const [regionQuery, setRegionQuery] = useState("");
  const [showCats, setShowCats] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // Live scan-progress popup (mirrors FMEA generation): activity log + elapsed timer.
  const [scanning, setScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState("");
  const [scanLog, setScanLog] = useState<{ t: string; phase: string; msg: string }[]>([]);
  const [scanStart, setScanStart] = useState(0);
  const [scanElapsed, setScanElapsed] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const scopeKey = `sub:${subId || "none"}`;

  // Nothing fetches on mount — the operator clicks "Load" to read the latest cached snapshot, or
  // "Run scan" to collect fresh. `loaded` gates the Azure-/server-hitting queries so opening the
  // page (or switching subscription) never auto-refreshes. A scan still lands its result via the
  // query cache (setQueryData), so results appear after a scan even without an explicit Load.
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setLoaded(false);
  }, [scopeKey, connId]);

  // Tick the elapsed-time counter while a scan is in flight.
  useEffect(() => {
    if (!scanning) return;
    const id = setInterval(() => setScanElapsed(Math.floor((Date.now() - scanStart) / 1000)), 1000);
    return () => clearInterval(id);
  }, [scanning, scanStart]);

  // Auto-scroll the activity log to the newest entry — WITHOUT moving the page. We scroll the
  // log's own container (scrollTop) instead of scrollIntoView, which would scroll every ancestor
  // (and yank the whole panel) when the snapshot below is tall.
  useEffect(() => {
    const el = logEndRef.current?.parentElement;
    if (el) el.scrollTop = el.scrollHeight;
  }, [scanLog]);

  const pushLog = useCallback((phase: string, m: string) => {
    const t = new Date().toLocaleTimeString([], { hour12: false });
    setScanStatus(m);
    setScanLog((prev) => [...prev.slice(-299), { t, phase, msg: m }]);
  }, []);

  const metaQ = useQuery<QuotaMeta>({ queryKey: ["quotaMeta"], queryFn: api.quotaMeta });
  const regionsQ = useQuery({
    queryKey: ["quotaRegions", connId, subId],
    queryFn: () => api.quotaRegions(subId, connId),
    // Region list loads once the user opens the region picker or has loaded/scanned this scope —
    // not automatically on page load.
    enabled: !!subId && (loaded || showRegions),
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
  });

  // Load the latest cached snapshot for the current scope on demand (no scan).
  function loadCached() {
    if (loaded) void overviewQ.refetch();
    else setLoaded(true);
  }

  // Streaming scan: shows a live progress popup (like FMEA), then lands the snapshot in cache.
  async function runScan() {
    if (scanning) return;
    setMsg(null);
    setScanning(true);
    setScanStatus("Starting…");
    setScanLog([]);
    setScanStart(Date.now());
    setScanElapsed(0);
    abortRef.current = new AbortController();
    pushLog("start", "🚀 Starting quota scan…");
    try {
      await streamQuotaScan(
        { subscription_id: subId, connection_id: connId, demo: false, regions: selRegions, categories: selCats, include_unused: showUnused },
        {
          onStatus: (s) => pushLog(s.phase, s.message),
          onDone: (fresh) => {
            qc.setQueryData(["quota", connId, scopeKey], fresh);
            setLoaded(true);
            qc.invalidateQueries({ queryKey: ["quotaRuns", subId] });
            if (fresh.error) {
              pushLog("error", `❌ ${fresh.error}`);
              setMsg({ text: fresh.error, ok: false });
            } else {
              setMsg({ text: `Scanned ${fresh.regions_scanned.length} region(s).`, ok: true });
            }
            setScanning(false);
          },
          onError: (m) => {
            pushLog("error", `❌ ${m}`);
            setMsg({ text: m, ok: false });
            setScanning(false);
          },
        },
        abortRef.current.signal,
      );
    } catch (e) {
      pushLog("error", `❌ ${formatError(e)}`);
      setMsg({ text: formatError(e), ok: false });
      setScanning(false);
    }
  }

  function cancelScan() {
    abortRef.current?.abort();
    pushLog("cancel", "⏹️ Cancelled.");
    setScanning(false);
  }

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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = results.filter((r) => {
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
  }, [results, fRegion, fProvider, fCategory, fFamilyOnly, fRisk, fKind, query, usageMin, usageMax, sortKey, sortDir]);

  const counts = data?.counts ?? {};
  const unregistered = (data?.provider_registration ?? []).filter((p) => !p.registered);
  const neverLoaded = !!data?.never_loaded;
  const notConfigured = data && !data.connection_configured;
  const canScan = !!subId;

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
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
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
              onClick={() => void runScan()}
              disabled={scanning || !canScan}
              className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50"
            >
              {scanning ? "Scanning…" : "↻ Run scan"}
            </button>
            {subId ? (
              <div className="flex items-center gap-1">
                <a href={api.quotaExportUrl(subId, false, "csv")} className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50">CSV</a>
                <a href={api.quotaExportUrl(subId, false, "json")} className="rounded-lg border bg-white px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50">JSON</a>
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
          <div className="p-8 text-center text-sm text-gray-500">Loading…</div>
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
          <div className="p-8 text-center text-sm text-gray-500">Loading…</div>
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
              <span className="text-[11px] text-gray-400">{filtered.length} of {results.length}</span>
            </div>

            {/* Table */}
            <div className="overflow-x-auto rounded-lg border bg-white">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
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
                  {filtered.map((r, i) => (
                    <tr
                      key={`${r.region}|${r.provider_namespace}|${r.quota_name}|${r.sku_family}|${i}`}
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
                  ))}
                  {filtered.length === 0 && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-sm text-gray-400">No quota rows match the filters.</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            {/* History strip */}
            {(runsQ.data?.runs?.length ?? 0) > 1 && (
              <div className="mt-3 rounded-lg border bg-white p-3">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">Scan history</div>
                <div className="flex flex-wrap gap-2">
                  {runsQ.data!.runs.map((run) => (
                    <div key={run.id} className="rounded border px-2 py-1 text-[11px] text-gray-600" title={run.started_at ?? ""}>
                      <span className="text-red-600">{run.critical_count}C</span>{" "}
                      <span className="text-amber-600">{run.warning_count}W</span>{" "}
                      <span className="text-gray-400">· {run.regions.length}rgn</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Scan progress popup (live activity log, mirrors FMEA generation) */}
      {scanning && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-lg overflow-hidden rounded-xl border bg-white shadow-2xl">
            <div className="flex items-center gap-2 border-b bg-violet-50/70 px-4 py-3">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-violet-300 border-t-violet-600" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-violet-900">Scanning quota…</div>
                <div className="truncate text-xs text-violet-600">{scanStatus || "Working…"}</div>
              </div>
              <span className="shrink-0 tabular-nums text-xs text-violet-500">{fmtElapsed(scanElapsed)}</span>
            </div>
            <div className="max-h-72 overflow-y-auto bg-white px-4 py-2 font-mono text-[11px] leading-relaxed text-gray-600">
              {scanLog.map((l, i) => (
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

