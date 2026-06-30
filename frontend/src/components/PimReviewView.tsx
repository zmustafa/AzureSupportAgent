import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type PimFinding, type PimGroupKey, type PimOverview } from "../api";
import { Skeleton } from "../utils/perf";

const SEV_META: Record<string, { label: string; cls: string; dot: string; rank: number }> = {
  critical: { label: "Critical", cls: "bg-red-100 text-red-700", dot: "bg-red-500", rank: 4 },
  error: { label: "Error", cls: "bg-orange-100 text-orange-700", dot: "bg-orange-500", rank: 3 },
  warning: { label: "Warning", cls: "bg-amber-100 text-amber-700", dot: "bg-amber-500", rank: 2 },
  info: { label: "Info", cls: "bg-sky-100 text-sky-700", dot: "bg-sky-500", rank: 1 },
  ok: { label: "OK", cls: "bg-green-100 text-green-700", dot: "bg-green-500", rank: 0 },
};

const TIER_META: Record<string, { label: string; cls: string }> = {
  tier0: { label: "Tier-0", cls: "bg-red-50 text-red-700 border-red-200" },
  tier1: { label: "Tier-1", cls: "bg-orange-50 text-orange-700 border-orange-200" },
  tier2: { label: "Tier-2", cls: "bg-gray-50 text-gray-600 border-gray-200" },
};

const ASSIGN_META: Record<string, { label: string; cls: string }> = {
  active: { label: "Standing (active)", cls: "bg-red-50 text-red-700" },
  eligible: { label: "Eligible (JIT)", cls: "bg-sky-50 text-sky-700" },
  activated: { label: "Activated", cls: "bg-violet-50 text-violet-700" },
};

const GROUPS: { key: PimGroupKey; label: string; icon: string; blurb: string }[] = [
  { key: "standing_access", label: "Eligible-vs-active drift", icon: "♾️", blurb: "Permanent/active assignments to privileged roles that should be Just-In-Time eligible instead." },
  { key: "stale_eligible", label: "Stale eligible roles", icon: "💤", blurb: "Eligible assignments never (or not recently) activated — unused standing privilege to prune." },
  { key: "stale_active", label: "Dormant privileged access", icon: "🕳️", blurb: "Active assignments idle for a long time — privilege nobody is exercising." },
  { key: "activation_review", label: "JIT activation review", icon: "⏱️", blurb: "Recent / currently-active activations to review for justification and duration." },
];

const KPIS: { key: keyof PimOverview["kpis"]; label: string }[] = [
  { key: "standing_access", label: "Standing access" },
  { key: "high_priv_standing", label: "Tier-0 standing" },
  { key: "stale_eligible", label: "Stale eligible" },
  { key: "stale_active", label: "Dormant active" },
  { key: "activations", label: "Activations" },
];

function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function SevBadge({ sev }: { sev: string }) {
  const m = SEV_META[sev] ?? SEV_META.info;
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${m.cls}`}>{m.label}</span>;
}

function IdleBadge({ f }: { f: PimFinding }) {
  if (f.assignment_type === "activated" && f.days_left != null) {
    const active = f.days_left >= 0;
    return (
      <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${active ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-500"}`}>
        {active ? "active now" : "expired"}
      </span>
    );
  }
  if (f.days_idle == null) return null;
  const d = f.days_idle;
  const cls = d >= 180 ? "bg-red-100 text-red-700" : d >= 90 ? "bg-amber-100 text-amber-700" : "bg-sky-100 text-sky-700";
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>idle {d}d</span>;
}

function FindingRow({ f }: { f: PimFinding }) {
  const tier = TIER_META[f.role_tier] ?? TIER_META.tier2;
  const assign = ASSIGN_META[f.assignment_type];
  return (
    <div className="border-t px-3 py-2 first:border-t-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <SevBadge sev={f.severity} />
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${tier.cls}`} title={`${f.role} privilege tier`}>{tier.label}</span>
        {assign && <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${assign.cls}`}>{assign.label}</span>}
        <IdleBadge f={f} />
        <span className="ml-auto text-[11px] text-gray-400">{f.scope}</span>
      </div>
      <div className="mt-1 text-[13px] font-medium text-gray-900">{f.title}</div>
      <div className="text-[12px] text-gray-600">{f.detail}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
        <span>👤 {f.subject} <span className="text-gray-400">({f.subject_id})</span></span>
        {f.activation_count_90d != null && <span>· {f.activation_count_90d} activation(s)/90d</span>}
      </div>
      {f.remediation && (
        <div className="mt-1 rounded bg-gray-50 px-2 py-1 text-[11px] text-gray-600">
          <span className="font-medium text-gray-700">Fix: </span>{f.remediation}
        </div>
      )}
    </div>
  );
}

export function PimReviewPanel({ connectionId = null }: { connectionId?: string | null }) {
  const qc = useQueryClient();
  const q = useQuery<PimOverview>({
    queryKey: ["pimOverview", connectionId],
    queryFn: () => api.pimOverview(connectionId),
    staleTime: 30_000,
  });
  const refresh = useMutation({
    mutationFn: () => api.refreshPim(connectionId),
    onSuccess: (data) => qc.setQueryData(["pimOverview", connectionId], data),
  });

  const data = q.data;
  const neverLoaded = data?.never_loaded;
  const source = data?.meta?.source;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
              <span>🔑</span> PIM / JIT lifecycle review
            </h2>
            <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">
              Eligible-vs-active role drift, stale privileged access, and Just-In-Time activation
              review over time — the privilege that quietly becomes permanent.
            </p>
          </div>
          <div className="flex items-center gap-3">
            {data && !neverLoaded && (
              <span className="text-[11px] text-gray-400">
                {source === "demo" ? "demo · " : source === "live" ? "live · " : ""}
                updated {agoText(data.age_seconds)}
                {data.stale && <span className="ml-1 rounded bg-amber-100 px-1 text-amber-700">stale</span>}
              </span>
            )}
            <button
              onClick={() => refresh.mutate()}
              disabled={refresh.isPending}
              className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {refresh.isPending ? "Reviewing…" : "Refresh"}
            </button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-gray-50 p-5">
        {q.isLoading ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              {[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-14 w-full" />)}
            </div>
            <Skeleton className="h-64 w-full" />
          </div>
        ) : q.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            Couldn&apos;t load the PIM review. You may not have the{" "}
            <code className="rounded bg-red-100 px-1">identity.read</code> permission.
            <button onClick={() => q.refetch()} className="ml-2 underline">Retry</button>
          </div>
        ) : neverLoaded ? (
          <div className="rounded-lg border bg-white p-8 text-center">
            <div className="text-sm text-gray-600">No PIM snapshot yet for this scope.</div>
            <div className="mt-1 text-[12px] text-gray-400">
              Press Refresh to review privileged-role drift, stale access and JIT activations.
            </div>
            <button
              onClick={() => refresh.mutate()}
              disabled={refresh.isPending}
              className="mt-3 rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {refresh.isPending ? "Reviewing…" : "Run PIM review"}
            </button>
          </div>
        ) : data ? (
          <div className="space-y-4">
            {/* KPIs */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              {KPIS.map(({ key, label }) => (
                <div key={key} className="rounded-lg border bg-white px-3 py-2">
                  <div className={`text-xl font-semibold ${data.kpis[key] ? "text-gray-900" : "text-gray-400"}`}>
                    {data.kpis[key]}
                  </div>
                  <div className="truncate text-[11px] text-gray-500">{label}</div>
                </div>
              ))}
            </div>

            {/* Groups */}
            {GROUPS.map(({ key, label, icon, blurb }) => {
              const items = data.groups[key] ?? [];
              const err = data.errors[key];
              const sev = data.group_severity[key];
              return (
                <div key={key} className="overflow-hidden rounded-lg border bg-white">
                  <div className="flex items-center gap-2 border-b bg-gray-50 px-3 py-2">
                    <span>{icon}</span>
                    <span className="text-sm font-medium text-gray-800">{label}</span>
                    <span className={`h-2 w-2 rounded-full ${SEV_META[sev]?.dot ?? SEV_META.ok.dot}`} />
                    <span className="ml-1 rounded-full bg-gray-100 px-2 text-[11px] text-gray-600">{items.length}</span>
                    <span className="ml-auto hidden truncate text-[11px] text-gray-400 sm:block">{blurb}</span>
                  </div>
                  {err && (
                    <div className="border-b bg-amber-50 px-3 py-1.5 text-[11px] text-amber-700">{err}</div>
                  )}
                  {items.length === 0 && !err ? (
                    <div className="px-3 py-3 text-[12px] text-gray-400">No findings in this group. 🎉</div>
                  ) : (
                    <div>{items.map((f) => <FindingRow key={f.id} f={f} />)}</div>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
}
