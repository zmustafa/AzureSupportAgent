import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { AzureIcon } from "./AzureIcon";
import {
  api,
  streamPolicySimulate,
  type PolicyAssignment,
  type PolicyAuthorResult,
  type PolicyCoverage,
  type PolicyCoverageRunSummary,
  type PolicyDriftResult,
  type PolicyHandoff,
  type PolicyHandoffFinding,
  type PolicyTagHandoff,
  type PolicyInventory,
  type PolicySimStatus,
  type PolicySimulateReq,
  type PolicySimulateResult,
  type PolicyTagGovernance,
  type PolicyTriageResult,
  type PolicyWhatIf,
} from "../api";
import { POLICY_NAV, type PolicyTab } from "./navConfig";
import { ConnectionScopePicker } from "./ConnectionScopePicker";

function agoLabel(ts: number): string {
  if (!ts) return "";
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Human-friendly elapsed duration: "0.4s", "12.7s", "1m 05s".
function fmtDur(ms: number): string {
  if (ms < 0) ms = 0;
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const m = Math.floor(totalSec / 60);
  const s = Math.round(totalSec % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

type TrackedStep = PolicySimStatus & { startedAt: number; durationMs?: number };

// One finding's outcome inside a combined (multi-select) batch simulation.
type BatchItem = {
  finding: PolicyHandoffFinding;
  status: "pending" | "running" | "done" | "error";
  res?: PolicySimulateResult;
  err?: string;
  steps?: TrackedStep[];
};

function verdictTone(v: string): string {
  const x = (v || "").toLowerCase();
  return x === "go" ? "bg-green-100 text-green-700" : x === "hold" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700";
}

const EFFECT_TONE: Record<string, string> = {
  deny: "bg-red-100 text-red-700",
  audit: "bg-amber-100 text-amber-700",
  auditifnotexists: "bg-amber-100 text-amber-700",
  deployifnotexists: "bg-blue-100 text-blue-700",
  modify: "bg-violet-100 text-violet-700",
  append: "bg-teal-100 text-teal-700",
  disabled: "bg-gray-100 text-gray-500",
  parameterized: "bg-gray-100 text-gray-600",
};

function effectTone(e: string): string {
  return EFFECT_TONE[(e || "").toLowerCase()] ?? "bg-gray-100 text-gray-600";
}

export function PolicyPanel({ tab }: { tab: PolicyTab }) {
  const navigate = useNavigate();
  const goTab = (t: PolicyTab) => navigate(`/policy/${t}`);
  const [connectionId, setConnectionId] = useState<string>("");
  const [withCompliance, setWithCompliance] = useState(false);
  // The selected workload scope is persisted (sessionStorage) so it survives a page refresh and
  // an explicit "Clear scope" stays cleared. "" = whole tenant/connection.
  const [workloadId, setWorkloadId] = useState<string>(() => {
    try { return sessionStorage.getItem("policyWorkloadId") ?? ""; } catch { return ""; }
  });
  const [handoff, setHandoff] = useState<PolicyHandoff | null>(null);
  const [tagHandoff, setTagHandoff] = useState<PolicyTagHandoff | null>(null);

  // Persist the scope choice (including an explicit clear) so a refresh restores exactly it.
  useEffect(() => {
    try { sessionStorage.setItem("policyWorkloadId", workloadId); } catch { /* ignore */ }
  }, [workloadId]);

  // Ingest a hand-off from the assessment report (sessionStorage): scope to the workload and
  // load the selected findings as a queue. We deliberately KEEP it in sessionStorage so a browser
  // refresh on the Rollout tab restores the same queue (the assessment routes straight to
  // /policy/rollout, so the tab itself is already preserved by the URL). A new hand-off overwrites it.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("policyHandoff");
      if (raw) {
        const h = JSON.parse(raw) as PolicyHandoff;
        if (h?.findings?.length) {
          // Scope is driven by the persisted `policyWorkloadId` (set when the hand-off was created),
          // NOT re-applied here — otherwise clearing the scope then refreshing would re-select it.
          setHandoff(h);
        }
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Ingest a hand-off from Tag Intelligence's policy generator: the generated tag policy
  // definitions, pre-loaded into the Rollout Planner's deploy mode as ready-to-simulate context.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("policyTagHandoff");
      if (raw) {
        const h = JSON.parse(raw) as PolicyTagHandoff;
        if (h?.definitions?.length) setTagHandoff(h);
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections, retry: false });
  const connections = connQ.data?.connections ?? [];
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, retry: false });
  const workloads = wlQ.data?.workloads ?? [];
  const selectedWorkload = workloads.find((w) => w.id === workloadId);
  // When scoped to a workload, align the Azure connection to that workload's connection.
  const effectiveConn = (selectedWorkload?.connection_id || connectionId || connections.find((c) => c.is_default)?.id || "");
  // Identity of the active scope — used to remount stateful tabs so none of their local
  // working state (typed intent, chosen scope, finding queue, simulation result) leaks
  // when the user switches connection or workload.
  const scopeKey = `${effectiveConn}|${workloadId}`;

  // The heavy inventory is cached PERMANENTLY SERVER-SIDE (per tenant + connection + workload +
  // compliance flag): the slow Azure round-trip runs only once until the user clicks Refresh /
  // Scan (which force a live pull). The client mirror never auto-expires (staleTime Infinity), so
  // navigating away and back, or a browser refresh, reuses the server cache without re-querying.
  const forceRef = useRef(false);
  const invQ = useQuery({
    queryKey: ["policyInventory", effectiveConn, withCompliance, workloadId],
    queryFn: async () => {
      const force = forceRef.current;
      forceRef.current = false;
      return api.policyInventory(effectiveConn || null, withCompliance, force, workloadId || null);
    },
    enabled: !connQ.isLoading, // wait for connections so we hit the real default once
    retry: false,
    staleTime: Infinity, // never auto-refetch; only Refresh/Scan re-pull from Azure
    gcTime: 60 * 60_000,
    placeholderData: keepPreviousData, // keep showing data while compliance toggles
  });
  const inv = invQ.data;
  // Prefer the server's "fetched_at" (when Azure was actually queried) over the client time.
  const updatedAt = inv?.fetched_at ? Date.parse(inv.fetched_at) : invQ.dataUpdatedAt;

  function refresh(scanCompliance = false) {
    forceRef.current = true;
    if (scanCompliance) {
      setWithCompliance(true); // key change triggers a forced fetch
      if (withCompliance) invQ.refetch();
    } else {
      invQ.refetch();
    }
  }

  return (
    <div className="flex h-full flex-col bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xl">🛡️</span>
            <h1 className="text-lg font-bold text-gray-800">Azure Policy</h1>
            <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[11px] font-medium text-brand">Governance toolkit</span>
          </div>
          <div className="flex items-center gap-2">
            {workloads.length > 0 && (
              <select
                value={workloadId}
                onChange={(e) => setWorkloadId(e.target.value)}
                className={`rounded-lg border px-2 py-1.5 text-xs ${workloadId ? "border-brand bg-brand/5 text-brand" : "border-gray-200 bg-white text-gray-700"}`}
                title="Scope everything to an Azure Workload (policies that govern it, inherited included)"
              >
                <option value="">🌐 All scopes</option>
                {workloads.map((w) => (
                  <option key={w.id} value={w.id}>🧩 {w.name}</option>
                ))}
              </select>
            )}
            {connections.length > 0 && (
              <ConnectionScopePicker
                value={effectiveConn}
                onChange={setConnectionId}
                disabled={!!workloadId}
                disabledTitle="Connection follows the selected workload"
              />
            )}
            <button
              onClick={() => refresh(true)}
              disabled={invQ.isFetching}
              className="rounded-lg border px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              title="Run a Policy Insights compliance scan (slower)"
            >
              {invQ.isFetching && withCompliance ? "Scanning…" : "🔎 Scan compliance"}
            </button>
            <button
              onClick={() => refresh(false)}
              disabled={invQ.isFetching}
              className="rounded-lg border px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              {invQ.isFetching && !withCompliance ? "Refreshing…" : "↻ Refresh"}
            </button>
            {inv && !inv.never_loaded && (
              invQ.isFetching ? (
                <span className="flex animate-pulse items-center gap-1 text-[11px] font-semibold text-brand">
                  <span className="inline-block h-2 w-2 animate-ping rounded-full bg-brand" />
                  Refreshing…
                </span>
              ) : updatedAt ? (
                <span className="text-[11px] text-gray-400" title={new Date(updatedAt).toLocaleString()}>
                  {`Updated ${agoLabel(updatedAt)}${inv.cached ? " · cached" : ""}`}
                </span>
              ) : null
            )}
          </div>
        </div>
        {/* Tabs */}
        <div className="mt-3 flex flex-wrap gap-1">
          {POLICY_NAV.map(({ id, label }) => (
            <button
              key={id}
              onClick={() => goTab(id)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                tab === id ? "bg-brand text-white" : "text-gray-600 hover:bg-gray-100"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Body — cached data always wins; only show the full loader when there's nothing yet. */}
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {!inv || inv.never_loaded ? (
          invQ.isError ? (
            <ErrorBox message={(invQ.error as Error)?.message || "Failed to load policy inventory."} />
          ) : invQ.isFetching ? (
            <Loading text="Loading policy inventory from Azure…" />
          ) : !inv ? (
            <Loading text="Loading…" />
          ) : (
            <div className="mx-auto max-w-2xl py-16 text-center">
              <div className="text-3xl">🛡️</div>
              <h2 className="mt-2 text-base font-semibold text-gray-900">Policy inventory not loaded yet</h2>
              <p className="mt-1 text-sm text-gray-500">
                Enumerating policy definitions, initiatives, assignments and exemptions across your
                scopes takes a moment, so it doesn&apos;t run automatically. Press Refresh to load it —
                it&apos;s then cached until you refresh again.
              </p>
              <button
                onClick={() => refresh(false)}
                disabled={invQ.isFetching}
                className="mt-4 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
              >
                {invQ.isFetching ? "Refreshing…" : "↻ Refresh"}
              </button>
            </div>
          )
        ) : (
          <>
            {(inv.workload || workloadId) && (
              <WorkloadBanner
                wl={inv.workload}
                name={selectedWorkload?.name}
                refreshing={invQ.isFetching}
                onClear={() => setWorkloadId("")}
              />
            )}
            {inv.errors?.length > 0 && <ErrorBox message={inv.errors.join(" · ")} soft />}
            {/* Key the stateful tabs on the active scope so switching connection/workload
                fully resets their local working state (typed intent, chosen scope, queued
                findings, in-progress simulation) instead of leaking it across scopes. */}
            {tab === "overview" && <Overview inv={inv} />}
            {tab === "inventory" && <Inventory inv={inv} />}
            {tab === "effective" && <Effective key={scopeKey} inv={inv} />}
            {tab === "advisors" && <Advisors key={scopeKey} inv={inv} connectionId={effectiveConn} />}
            {tab === "rollout" && <RolloutPlanner key={scopeKey} inv={inv} connectionId={effectiveConn} handoff={handoff} tagHandoff={tagHandoff} />}
            {tab === "ai" && <AiTools key={scopeKey} inv={inv} connectionId={effectiveConn} />}
            {tab === "drift" && <DriftIac key={scopeKey} inv={inv} />}
            {tab === "history" && <History key={scopeKey} connectionId={effectiveConn} />}
          </>
        )}
      </div>
    </div>
  );
}

// =========================================================================== Workload banner
function WorkloadBanner({ wl, name, refreshing, onClear }: {
  wl: PolicyInventory["workload"];
  name?: string;
  refreshing: boolean;
  onClear: () => void;
}) {
  // Always title from the live selection so the banner can't show a stale workload while the
  // newly-scoped inventory is still loading (keepPreviousData keeps the previous one on screen).
  const displayName = name || wl?.name || "…";
  // The counts belong to the resolved scope; while switching workloads they're momentarily for
  // the previous one, so treat them as not-yet-known until this refresh lands.
  const countsStale = refreshing && (!wl || (!!name && wl.name !== name));
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-brand/30 bg-brand/5 px-4 py-2.5">
      <span className="text-base">🧩</span>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <div className="text-sm font-semibold text-gray-800">Scoped to workload: {displayName}</div>
          {refreshing && (
            <span className="flex animate-pulse items-center gap-1 rounded-full bg-brand/10 px-1.5 py-0.5 text-[10px] font-semibold text-brand">
              <span className="inline-block h-1.5 w-1.5 animate-ping rounded-full bg-brand" />
              Refreshing…
            </span>
          )}
        </div>
        {countsStale ? (
          <div className="animate-pulse text-[11px] text-brand/70">Resolving scope &amp; governing policies…</div>
        ) : wl ? (
          <div className={`text-[11px] text-gray-500 ${refreshing ? "opacity-60" : ""}`}>
            {wl.subscription_count} subscription{wl.subscription_count === 1 ? "" : "s"} ·{" "}
            {wl.resource_group_count} resource group{wl.resource_group_count === 1 ? "" : "s"}
            {wl.resource_count > 0 ? ` · ${wl.resource_count} resource${wl.resource_count === 1 ? "" : "s"}` : ""}
            {wl.ancestor_management_groups.length > 0 && ` · MGs: ${wl.ancestor_management_groups.slice(0, 3).join(", ")}`}
            {" · "}showing policies that govern this workload (inherited included)
          </div>
        ) : (
          <div className="animate-pulse text-[11px] text-brand/70">Resolving scope &amp; governing policies…</div>
        )}
        {wl?.error && <div className="text-[11px] text-amber-600">{wl.error}</div>}
      </div>
      <button onClick={onClear} className="ml-auto shrink-0 rounded-lg border bg-white px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">
        🌐 Clear scope
      </button>
    </div>
  );
}

// =========================================================================== Overview
function Overview({ inv }: { inv: PolicyInventory }) {
  const byEffect = useMemo(() => {
    const m: Record<string, number> = {};
    for (const a of inv.assignments) {
      const e = a.effect || "unknown";
      m[e] = (m[e] ?? 0) + 1;
    }
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  }, [inv.assignments]);
  const dryRun = inv.assignments.filter((a) => a.enforcement_mode !== "Default").length;
  const adv = inv.advisors;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Kpi label="Definitions" value={inv.counts.definitions} sub={`${inv.counts.custom_definitions} custom`} icon="📜" />
        <Kpi label="Initiatives" value={inv.counts.initiatives} icon="🧩" />
        <Kpi label="Assignments" value={inv.counts.assignments} sub={dryRun ? `${dryRun} dry-run` : undefined} icon="📌" />
        <Kpi label="Exemptions" value={inv.counts.exemptions} icon="🪪" />
        <Kpi
          label="Non-compliant"
          value={inv.compliance.available ? inv.compliance.total_non_compliant_resources : "—"}
          sub={inv.compliance.available ? `${inv.compliance.subscriptions_scanned} subs` : "scan to see"}
          icon="⚠️"
        />
        <Kpi label="Scopes" value={inv.scope_tree.length} icon="🗂️" />
      </div>

      {/* Advisor highlights */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
        <HighlightCard
          tone="green"
          icon="🟢"
          title="Promote to Deny"
          value={adv.promote_to_deny.filter((p) => p.safe_to_promote).length}
          total={adv.promote_to_deny.length}
          desc="audit policies safe to enforce"
        />
        <HighlightCard
          tone="red"
          icon="🩹"
          title="Remediation gaps"
          value={adv.remediation_gaps.length}
          desc="DINE/modify with no identity"
        />
        <HighlightCard
          tone="amber"
          icon="🧹"
          title="Exemption issues"
          value={(adv.exemption_hygiene.buckets.expired ?? 0) + (adv.exemption_hygiene.buckets.never_expires ?? 0)}
          total={adv.exemption_hygiene.total}
          desc="expired / never-expiring"
        />
        <HighlightCard
          tone="violet"
          icon="⚖️"
          title="Conflicts"
          value={adv.conflicts.length}
          desc="duplicate / redundant assignments"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Assignments by effect" icon="🎯">
          {byEffect.length === 0 ? <Empty text="No assignments." /> : (
            <ul className="space-y-1.5">
              {byEffect.map(([eff, n]) => (
                <li key={eff} className="flex items-center gap-2 text-sm">
                  <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${effectTone(eff)}`}>{eff || "unknown"}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-100">
                    <div className="h-full rounded-full bg-brand/60" style={{ width: `${Math.round((100 * n) / inv.counts.assignments)}%` }} />
                  </div>
                  <span className="w-8 text-right text-xs text-gray-500">{n}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Scope hierarchy" icon="🗂️">
          {inv.scope_tree.length === 0 ? <Empty text="No assignments at any scope." /> : (
            <ul className="space-y-1">
              {inv.scope_tree.slice(0, 14).map((s) => (
                <li key={s.scope} className="flex items-center justify-between gap-2 text-sm" style={{ paddingLeft: s.depth * 12 }}>
                  <span className="truncate text-gray-700">
                    <ScopeIcon kind={s.kind} /> {s.label}
                  </span>
                  <span className="shrink-0 text-xs text-gray-400">{s.assignments} pol · {s.exemptions} exm</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

// =========================================================================== Inventory
function Inventory({ inv }: { inv: PolicyInventory }) {
  const [sub, setSub] = useState<"assignments" | "definitions" | "initiatives" | "exemptions">("assignments");
  const [q, setQ] = useState("");
  const ql = q.toLowerCase();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {([
          ["assignments", `Assignments (${inv.counts.assignments})`],
          ["definitions", `Definitions (${inv.counts.definitions})`],
          ["initiatives", `Initiatives (${inv.counts.initiatives})`],
          ["exemptions", `Exemptions (${inv.counts.exemptions})`],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            onClick={() => setSub(id)}
            className={`rounded-lg px-2.5 py-1 text-xs font-medium ${sub === id ? "bg-gray-800 text-white" : "border text-gray-600 hover:bg-gray-50"}`}
          >
            {label}
          </button>
        ))}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search…"
          className="ml-auto w-56 rounded-lg border border-gray-200 px-2.5 py-1 text-sm"
        />
      </div>

      <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
        {sub === "assignments" && (
          <Table head={["Assignment", "Scope", "Definition", "Effect", "Enforcement", "Identity"]}>
            {inv.assignments
              .filter((a) => !ql || `${a.display_name} ${a.definition_name} ${a.scope_label}`.toLowerCase().includes(ql))
              .slice(0, 300)
              .map((a) => (
                <tr key={a.id} className="border-t hover:bg-gray-50">
                  <Td>
                    <div className="font-medium text-gray-800">{a.display_name}</div>
                    {a.is_initiative && <span className="text-[10px] text-violet-600">initiative</span>}
                  </Td>
                  <Td><ScopeIcon kind={a.scope_kind} /> {a.scope_label}</Td>
                  <Td className="text-gray-600">{a.definition_name}</Td>
                  <Td><Pill cls={effectTone(a.effect)}>{a.effect || "—"}</Pill></Td>
                  <Td>{a.enforcement_mode === "Default" ? <span className="text-green-600">Enforced</span> : <span className="text-amber-600">Dry-run</span>}</Td>
                  <Td className="text-gray-500">{a.identity_type === "None" ? "—" : a.identity_type}</Td>
                </tr>
              ))}
          </Table>
        )}
        {sub === "definitions" && (
          <Table head={["Definition", "Type", "Category", "Effect", "Mode"]}>
            {inv.definitions
              .filter((d) => !ql || `${d.display_name} ${d.category}`.toLowerCase().includes(ql))
              .slice(0, 400)
              .map((d) => (
                <tr key={d.id} className="border-t hover:bg-gray-50">
                  <Td className="font-medium text-gray-800">{d.display_name}</Td>
                  <Td>{d.policy_type === "Custom" ? <Pill cls="bg-blue-100 text-blue-700">Custom</Pill> : <span className="text-gray-500">Built-in</span>}</Td>
                  <Td className="text-gray-600">{d.category}</Td>
                  <Td><Pill cls={effectTone(d.effect)}>{d.effect || "—"}</Pill></Td>
                  <Td className="text-gray-500">{d.mode}</Td>
                </tr>
              ))}
          </Table>
        )}
        {sub === "initiatives" && (
          <Table head={["Initiative", "Type", "Category", "Policies"]}>
            {inv.initiatives
              .filter((i) => !ql || `${i.display_name} ${i.category}`.toLowerCase().includes(ql))
              .map((i) => (
                <tr key={i.id} className="border-t hover:bg-gray-50">
                  <Td className="font-medium text-gray-800">{i.display_name}</Td>
                  <Td>{i.policy_type === "Custom" ? <Pill cls="bg-blue-100 text-blue-700">Custom</Pill> : <span className="text-gray-500">Built-in</span>}</Td>
                  <Td className="text-gray-600">{i.category}</Td>
                  <Td className="text-gray-500">{i.policy_count}</Td>
                </tr>
              ))}
          </Table>
        )}
        {sub === "exemptions" && (
          <Table head={["Exemption", "Scope", "Category", "Expires"]}>
            {inv.exemptions
              .filter((e) => !ql || `${e.display_name} ${e.scope_label}`.toLowerCase().includes(ql))
              .map((e) => (
                <tr key={e.id} className="border-t hover:bg-gray-50">
                  <Td className="font-medium text-gray-800">{e.display_name}</Td>
                  <Td><ScopeIcon kind={e.scope_kind} /> {e.scope_label}</Td>
                  <Td className="text-gray-600">{e.category}</Td>
                  <Td className="text-gray-500">{e.expires_on ? new Date(e.expires_on).toLocaleDateString() : "Never"}</Td>
                </tr>
              ))}
          </Table>
        )}
      </div>
    </div>
  );
}

// =========================================================================== Effective
/** Azure portal deep link for a policy scope (ARM id). Management groups use the MG
 *  drill-down blade; subscriptions / resource groups / resources use the generic resource
 *  overview, which the portal resolves for every ARM scope. Returns "" for an empty scope. */
function scopePortalUrl(scope: string): string {
  const s = (scope || "").trim();
  if (!s) return "";
  const mg = s.match(/\/managementGroups\/([^/]+)/i);
  if (mg) {
    return `https://portal.azure.com/#view/Microsoft_Azure_ManagementGroups/ManagementGroupDrillDownMenuBlade/~/overview/tenantId//mgId/${encodeURIComponent(mg[1])}`;
  }
  return `https://portal.azure.com/#@/resource${s}/overview`;
}

function Effective({ inv }: { inv: PolicyInventory }) {
  const [scope, setScope] = useState("");
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.policyEffective>> | null>(null);
  const [busy, setBusy] = useState(false);

  async function resolve(s: string) {
    if (!s) return;
    setScope(s);
    setBusy(true);
    try {
      setResult(await api.policyEffective(s, inv.assignments, inv.exemptions));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card title="Effective-policy resolver" icon="🔬">
        <p className="mb-3 text-xs text-gray-500">
          Pick any scope to see every policy that actually applies after inheritance, <code>notScopes</code> exclusions, and exemptions.
        </p>
        <div className="flex flex-wrap gap-2">
          <select
            value={scope}
            onChange={(e) => resolve(e.target.value)}
            className="min-w-64 rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
          >
            <option value="">Select a scope…</option>
            {inv.scope_tree.map((s) => (
              <option key={s.scope} value={s.scope}>{"  ".repeat(s.depth)}{s.label}</option>
            ))}
          </select>
          <input
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && resolve(scope)}
            placeholder="…or paste a scope id (/subscriptions/…/resourceGroups/…)"
            className="min-w-72 flex-1 rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
          />
          <button onClick={() => resolve(scope)} disabled={busy || !scope} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            Resolve
          </button>
          <a
            href={scope ? scopePortalUrl(scope) : undefined}
            target="_blank"
            rel="noopener noreferrer"
            aria-disabled={!scope}
            onClick={(e) => { if (!scope) e.preventDefault(); }}
            title="Open this scope in the Azure portal"
            className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
              scope ? "border-gray-200 text-gray-600 hover:bg-gray-50" : "cursor-not-allowed border-gray-200 text-gray-300"
            }`}
          >
            Open in Azure ↗
          </a>
        </div>
      </Card>

      {result && (
        <Card title={`${result.count} effective ${result.count === 1 ? "policy" : "policies"} at ${result.scope_label}`} icon="✅">
          {result.effective.length === 0 ? <Empty text="No policies apply at this scope." /> : (
            <Table head={["Policy", "Effect", "Source", "Enforcement", "Exemptions"]}>
              {result.effective.map((e) => (
                <tr key={e.id} className="border-t">
                  <Td className="font-medium text-gray-800">{e.display_name}</Td>
                  <Td><Pill cls={effectTone(e.effect)}>{e.effect || "—"}</Pill></Td>
                  <Td className="text-gray-600">{e.is_inherited ? `↑ ${e.inherited_from}` : "this scope"}</Td>
                  <Td>{e.enforcement_mode === "Default" ? "Enforced" : "Dry-run"}</Td>
                  <Td className="text-gray-500">{e.exemptions.length ? `${e.exemptions.length} exempt` : "—"}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      )}
    </div>
  );
}

// =========================================================================== Advisors
function Advisors({ inv, connectionId }: { inv: PolicyInventory; connectionId: string }) {
  const adv = inv.advisors;
  return (
    <div className="space-y-4">
      {/* Promote to Deny */}
      <Card title="🟢 Promote to Deny" icon="" subtitle="Audit policies that are 100% compliant today — flipping to deny blocks nothing.">
        {adv.promote_to_deny.length === 0 ? <Empty text="No audit-effect assignments found." /> : (
          <div className="space-y-1.5">
            {!inv.compliance.available && (
              <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
                Run a <b>compliance scan</b> (top-right) to confirm zero-breakage candidates.
              </div>
            )}
            {adv.promote_to_deny.slice(0, 20).map((p) => (
              <div key={p.assignment_id} className={`flex items-start justify-between gap-3 rounded-lg border p-2.5 ${p.safe_to_promote ? "border-green-200 bg-green-50/40" : ""}`}>
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-800">{p.display_name}</div>
                  <div className="text-[11px] text-gray-500">{p.scope_label} · {p.reason}</div>
                </div>
                {p.safe_to_promote ? <Pill cls="bg-green-100 text-green-700">safe ✓</Pill>
                  : p.compliance_unknown ? <Pill cls="bg-gray-100 text-gray-500">unknown</Pill>
                  : <Pill cls="bg-red-100 text-red-700">{p.non_compliant_resources} blocked</Pill>}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Remediation gaps */}
      <Card title="🩹 Remediation gap finder" icon="" subtitle="deployIfNotExists / modify assignments with no managed identity — remediation can never run.">
        {adv.remediation_gaps.length === 0 ? <Empty text="No remediation gaps. 🎉" /> : (
          <div className="space-y-1.5">
            {adv.remediation_gaps.map((g) => (
              <div key={g.assignment_id} className="rounded-lg border border-red-200 bg-red-50/40 p-2.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">{g.display_name}</span>
                  <Pill cls={effectTone(g.effect)}>{g.effect}</Pill>
                  <span className="text-[11px] text-gray-500">{g.scope_label}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-red-700">{g.issue}</div>
                <div className="text-[11px] text-gray-500">Fix: {g.fix}</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Exemption hygiene */}
      <Card title="🧹 Exemption hygiene" icon="" subtitle="Expired, never-expiring, or unjustified exemptions are an audit & security risk.">
        <div className="mb-2 flex flex-wrap gap-2 text-[11px]">
          {Object.entries(adv.exemption_hygiene.buckets).map(([k, v]) => (
            <span key={k} className={`rounded-full px-2 py-0.5 ${k === "expired" ? "bg-red-100 text-red-700" : k === "expiring_soon" ? "bg-amber-100 text-amber-700" : k === "healthy" ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-600"}`}>
              {k.replace(/_/g, " ")}: {v}
            </span>
          ))}
        </div>
        {adv.exemption_hygiene.items.length === 0 ? <Empty text="No exemptions." /> : (
          <div className="space-y-1">
            {adv.exemption_hygiene.items.filter((i) => i.status !== "healthy").slice(0, 20).map((i) => (
              <div key={i.id} className="flex items-center justify-between gap-2 rounded-lg border p-2 text-sm">
                <div className="min-w-0">
                  <span className="font-medium text-gray-800">{i.display_name}</span>
                  <span className="ml-2 text-[11px] text-gray-500">{i.scope_label}</span>
                </div>
                <div className="flex gap-1">
                  {i.flags.map((f) => (
                    <span key={f} className={`rounded px-1.5 py-0.5 text-[10px] ${f === "expired" ? "bg-red-100 text-red-700" : f === "unjustified" ? "bg-orange-100 text-orange-700" : "bg-gray-100 text-gray-600"}`}>{f.replace(/_/g, " ")}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Conflicts */}
      <Card title="⚖️ Conflict & redundancy detector" icon="" subtitle="The same policy assigned at multiple scopes or duplicated — consolidate.">
        {adv.conflicts.length === 0 ? <Empty text="No conflicts or duplicates." /> : (
          <div className="space-y-2">
            {adv.conflicts.map((c) => (
              <div key={c.policy_definition_id} className="rounded-lg border p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">{c.definition_name}</span>
                  <Pill cls={c.kind === "duplicate_same_scope" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{c.kind.replace(/_/g, " ")}</Pill>
                  <Pill cls="bg-gray-100 text-gray-500">{c.is_initiative ? "Initiative" : "Policy"}</Pill>
                  {c.category && <span className="text-[11px] text-gray-500">{c.category}</span>}
                  <span className="ml-auto text-[11px] text-gray-500">
                    {c.assignment_count} assignment{c.assignment_count === 1 ? "" : "s"}
                    {c.scope_count != null && c.scope_count !== c.assignment_count ? ` · ${c.scope_count} scope${c.scope_count === 1 ? "" : "s"}` : ""}
                  </span>
                </div>
                {/* Per-assignment detail rows */}
                <div className="mt-2 divide-y divide-gray-100 rounded-md border border-gray-100 bg-gray-50/50">
                  {c.scopes.map((s, i) => (
                    <div key={s.id ?? i} className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2.5 py-1.5 text-[11px]">
                      <ScopeIcon kind={s.scope_kind ?? ""} />
                      <span className="font-medium text-gray-700">{s.label}</span>
                      {s.assignment_name && s.assignment_name !== c.definition_name && (
                        <span className="text-gray-500" title="Assignment name">· {s.assignment_name}</span>
                      )}
                      {s.effect && <Pill cls={effectTone(s.effect)}>{s.effect}</Pill>}
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${s.enforcement_mode === "DoNotEnforce" ? "bg-gray-200 text-gray-600" : "bg-green-100 text-green-700"}`}>
                        {s.enforcement_mode === "DoNotEnforce" ? "Dry-run" : "Enforced"}
                      </span>
                      {s.scope && (
                        <a
                          href={scopePortalUrl(s.scope)}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          title="Open this scope in the Azure portal"
                          className="ml-auto shrink-0 text-gray-400 transition hover:text-brand"
                        >
                          Open in Azure ↗
                        </a>
                      )}
                    </div>
                  ))}
                </div>
                <div className="mt-1.5 text-[11px] text-gray-600">{c.hint}</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <CoverageAdvisor inv={inv} connectionId={connectionId} />
    </div>
  );
}

function CoverageAdvisor({ inv, connectionId }: { inv: PolicyInventory; connectionId: string }) {
  const qc = useQueryClient();
  const baseQ = useQuery({ queryKey: ["policyBaselines"], queryFn: api.policyBaselines });
  const workloadId = inv.workload?.id || "";
  const workloadName = inv.workload?.name || "";
  const [baselineId, setBaselineId] = useState("waf");
  const [result, setResult] = useState<PolicyCoverage | null>(null);
  const [busy, setBusy] = useState(false);
  const [viewingId, setViewingId] = useState("");
  const [fromHistory, setFromHistory] = useState(false);
  const resultRef = useRef<HTMLDivElement | null>(null);

  // History of previous Coverage-gap analyses for the selected scope — every run is persisted
  // server-side so a prior one can be reopened and reviewed even after navigating away. Keyed on
  // the workload so switching scope shows only that scope's history.
  const runsQ = useQuery({
    queryKey: ["policyCoverageRuns", workloadId],
    queryFn: () => api.policyCoverageRuns(workloadId || null),
  });
  const savedRuns = runsQ.data?.runs ?? [];

  // When the scope (workload) changes, clear the displayed analysis so we never show a result that
  // belongs to the previously-selected scope. The history grid below reloads via its query key.
  useEffect(() => {
    setResult(null);
    setViewingId("");
    setFromHistory(false);
  }, [workloadId]);

  async function run() {
    setBusy(true);
    try {
      const r = await api.policyCoverage(baselineId, inv.assignments, inv.definitions, true, workloadId, workloadName, connectionId);
      setResult(r);
      setViewingId(r.id || "");
      setFromHistory(false);
      qc.invalidateQueries({ queryKey: ["policyCoverageRuns"] });
    } finally {
      setBusy(false);
    }
  }

  // Reopen a previously-saved analysis exactly as it was.
  async function openSaved(id: string) {
    try {
      const { run: rec } = await api.policyCoverageRun(id);
      setResult(rec.result);
      setBaselineId(rec.baseline_id || baselineId);
      setViewingId(id);
      setFromHistory(true);
      requestAnimationFrame(() => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
    } catch {
      /* ignore */
    }
  }

  async function deleteSaved(id: string) {
    try {
      await api.policyDeleteCoverageRun(id);
      if (viewingId === id) { setResult(null); setViewingId(""); }
      qc.invalidateQueries({ queryKey: ["policyCoverageRuns"] });
    } catch {
      /* ignore */
    }
  }

  const viewing = savedRuns.find((r) => r.id === viewingId);
  const covPctTone = (p: number) => (p >= 80 ? "bg-green-100 text-green-700" : p >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700");

  return (
    <Card title="📐 Coverage-gap analysis" icon="" subtitle="Compare your tenant against a best-practice baseline; AI proposes built-ins to close gaps.">
      <div className="flex flex-wrap items-center gap-2">
        <select value={baselineId} onChange={(e) => setBaselineId(e.target.value)} className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
          {(baseQ.data?.baselines ?? []).map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
        </select>
        <button onClick={run} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
          {busy ? "Analyzing…" : "Analyze coverage"}
        </button>
      </div>

      {result && (
        <div ref={resultRef} className="mt-3 space-y-3">
          {viewing && fromHistory && (
            <div className="flex items-center gap-2 rounded-lg bg-gray-50 px-2.5 py-1.5 text-[11px] text-gray-500">
              🕑 Viewing a saved analysis from <b className="text-gray-700">{new Date(viewing.created_at).toLocaleString()}</b> · run a new analysis to refresh.
            </div>
          )}
          <div className="flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-100">
              <div className="h-full rounded-full bg-green-500" style={{ width: `${result.coverage_pct}%` }} />
            </div>
            <span className="text-sm font-semibold text-gray-700">{result.coverage_pct}% covered</span>
          </div>
          {result.missing.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Missing controls ({result.missing_count})</div>
              <div className="space-y-1.5">
                {result.missing.map((m) => {
                  const prop = result.proposals.find((p) => p.control_id === m.id);
                  return (
                    <div key={m.id} className="rounded-lg border border-amber-200 bg-amber-50/40 p-2.5">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-gray-800">{m.title}</span>
                        <Pill cls="bg-gray-100 text-gray-600">{m.domain}</Pill>
                        <Pill cls={effectTone(m.effect)}>{m.effect}</Pill>
                      </div>
                      {prop && (
                        <div className="mt-1 text-[11px] text-gray-600">
                          → Assign <b>{prop.builtin_policy}</b> ({prop.effect}) at <b>{prop.assign_at}</b>. {prop.why}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Analysis history grid — every Coverage-gap analysis run for the selected scope. Click a
          row to reopen it above; nothing here is applied to Azure. */}
      <div className="mt-4 border-t pt-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-500">
            🕑 Analysis history
            <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">{savedRuns.length}</span>
          </h4>
          <span className="text-[11px] text-gray-400">
            {workloadId ? <>Scope: <b className="text-gray-600">🧩 {workloadName}</b></> : "🌐 All scopes"}
          </span>
        </div>
        {savedRuns.length === 0 ? (
          <div className="rounded-lg border border-dashed bg-gray-50/60 p-4 text-center text-xs text-gray-400">
            {runsQ.isLoading ? "Loading history…" : "No coverage analyses yet for this scope. Run one above to start building history."}
          </div>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-[10px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2 text-left font-semibold">Baseline</th>
                  {!workloadId && <th className="px-2 py-2 text-left font-semibold">Scope</th>}
                  <th className="px-2 py-2 text-center font-semibold">Coverage</th>
                  <th className="px-2 py-2 text-center font-semibold">Covered</th>
                  <th className="px-2 py-2 text-center font-semibold">Missing</th>
                  <th className="px-2 py-2 text-center font-semibold">Proposals</th>
                  <th className="px-3 py-2 text-left font-semibold">When</th>
                  <th className="px-2 py-2 text-right font-semibold"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {savedRuns.map((r: PolicyCoverageRunSummary) => (
                  <tr
                    key={r.id}
                    onClick={() => openSaved(r.id)}
                    className={`cursor-pointer ${viewingId === r.id ? "bg-brand/5" : "hover:bg-gray-50"}`}
                  >
                    <td className="px-3 py-2 font-medium text-gray-800">{r.baseline_label || r.baseline_id}</td>
                    {!workloadId && <td className="px-2 py-2 text-[11px] text-gray-500">{r.workload_name ? `🧩 ${r.workload_name}` : "🌐 All"}</td>}
                    <td className="px-2 py-2 text-center">
                      <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${covPctTone(r.coverage_pct)}`}>{r.coverage_pct}%</span>
                    </td>
                    <td className="px-2 py-2 text-center text-gray-600">{r.covered_count}</td>
                    <td className="px-2 py-2 text-center text-gray-600">{r.missing_count}</td>
                    <td className="px-2 py-2 text-center text-gray-600">{r.proposals_count}</td>
                    <td className="px-3 py-2 whitespace-nowrap text-[11px] text-gray-500">{new Date(r.created_at).toLocaleString()}</td>
                    <td className="px-2 py-2 text-right">
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteSaved(r.id); }}
                        title="Delete saved analysis"
                        className="rounded p-1 text-gray-300 hover:bg-red-50 hover:text-red-500"
                      >
                        🗑
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

// =========================================================================== Rollout Planner
const TARGET_EFFECTS = ["audit", "auditIfNotExists", "deny", "denyAction", "append", "deployIfNotExists", "modify", "disabled"];

function RolloutPlanner({ inv, connectionId, handoff, tagHandoff }: { inv: PolicyInventory; connectionId: string; handoff?: PolicyHandoff | null; tagHandoff?: PolicyTagHandoff | null }) {
  const [step, setStep] = useState(1);
  const [mode, setMode] = useState<"promote" | "deploy" | "finding">("promote");
  // promote
  const [assignmentId, setAssignmentId] = useState("");
  const selected = inv.assignments.find((a) => a.id === assignmentId);
  // deploy
  const [intent, setIntent] = useState("");
  const [policyJson, setPolicyJson] = useState("");
  // finding (assessment hand-off)
  const [queue, setQueue] = useState<PolicyHandoffFinding[]>([]);
  const [activeFindingIdx, setActiveFindingIdx] = useState(0);
  const activeFinding = queue[activeFindingIdx] || null;
  // target
  const [scope, setScope] = useState("");
  const [effect, setEffect] = useState("deny");
  const [enforcement, setEnforcement] = useState("Default");
  // result
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<PolicySimulateResult | null>(null);
  const [err, setErr] = useState("");
  const [steps, setSteps] = useState<TrackedStep[]>([]);
  const [activeKey, setActiveKey] = useState("");
  const [startedAt, setStartedAt] = useState(0);
  const [now, setNow] = useState(0);
  const [savedChecks, setSavedChecks] = useState<Set<string>>(new Set());
  // Multi-select batch: which queued findings to simulate together, plus per-finding outcomes.
  const [selectedFindings, setSelectedFindings] = useState<Set<string>>(new Set());
  const [batch, setBatch] = useState<BatchItem[] | null>(null);
  const [batchActive, setBatchActive] = useState(-1);
  const [batchStartedAt, setBatchStartedAt] = useState(0);
  const stepsRef = useRef<TrackedStep[]>([]);
  // Abort controller for the in-flight simulation stream (Stop button / unmount cleanup),
  // plus a wall-clock safety timeout so a hung AI phase can't spin forever.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);
  // Saved simulations: every completed run is persisted (below) so a previous one can be reopened.
  const qc = useQueryClient();
  const wlId = handoff?.workload_id || inv.workload?.id || "";
  const savedSimsQ = useQuery({
    queryKey: ["policySimulations", wlId],
    queryFn: () => api.policySimulations(wlId || null),
    retry: false,
  });
  const savedSims = savedSimsQ.data?.simulations ?? [];
  const [viewingSavedAt, setViewingSavedAt] = useState("");
  const [showSaved, setShowSaved] = useState(true);
  // Tick a clock while running so the active step + total elapsed update live.
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(id);
  }, [busy]);

  // Ingest the assessment hand-off: switch to finding mode, load the queue + defaults. Kept
  // resilient — the handoff stays in the parent, so if this panel remounts (inventory re-scope
  // or tab switch) the queue is restored rather than silently lost.
  useEffect(() => {
    if (handoff?.findings?.length) {
      setMode("finding");
      setQueue(handoff.findings);
      setSelectedFindings(new Set(handoff.findings.map((f) => f.check_id)));
      setActiveFindingIdx(0);
      setStep(1);
      const f0 = handoff.findings[0];
      if (f0?.suggested_effect) setEffect(f0.suggested_effect);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handoff]);

  // Ingest the Tag Intelligence hand-off: switch to deploy mode and pre-load the generated tag
  // policy definitions, with the first one's JSON + intent + effect ready to simulate. A small
  // selector (rendered below) lets the user switch which generated definition is loaded.
  const tagDefs = tagHandoff?.definitions ?? [];
  const [tagDefIdx, setTagDefIdx] = useState(0);
  function loadTagDef(i: number) {
    const d = tagDefs[i];
    if (!d) return;
    setTagDefIdx(i);
    setMode("deploy");
    setIntent(`${d.displayName} (generated from your tag usage by Tag Intelligence)`);
    setPolicyJson(d.json);
    if (d.effect) setEffect(d.effect);
    setStep(1);
  }
  useEffect(() => {
    if (tagDefs.length) loadTagDef(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tagHandoff]);

  // The hand-off can land before the workload-scoped inventory has loaded (keepPreviousData
  // still shows the wider tenant view), which would leave the target scope empty and Simulate
  // disabled. Re-assert a sensible default scope once the workload resolves. Applies to the
  // assessment finding hand-off and the Tag Intelligence deploy hand-off.
  useEffect(() => {
    const handoffActive = mode === "finding" || (mode === "deploy" && tagDefs.length > 0);
    if (!handoffActive || scope) return;
    const s = inv.workload?.scope_ids?.[0] || inv.scope_tree?.[0]?.scope || "";
    if (s) setScope(s);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, scope, inv]);

  // Pre-seed which findings already have a saved guardrail (loop closed), so the queue shows
  // "\uD83D\uDEE1 planned" without re-running them and stays consistent with the report.
  const plannedLinksQ = useQuery({
    queryKey: ["policyEnforcementLinks", handoff?.workload_id || inv.workload?.id || ""],
    queryFn: () => api.policyEnforcementLinks(handoff?.workload_id || inv.workload?.id || null),
    enabled: !!handoff,
    retry: false,
  });
  useEffect(() => {
    const ids = (plannedLinksQ.data?.links ?? []).map((l) => l.check_id);
    if (ids.length) setSavedChecks((prev) => new Set([...prev, ...ids]));
  }, [plannedLinksQ.data]);

  const ncByAsg = inv.compliance?.by_assignment ?? {};
  const selectedNc = selected ? ncByAsg[selected.id.toLowerCase()]?.non_compliant_resources ?? -1 : -1;

  const canRun =
    !!scope &&
    (mode === "promote" ? !!selected : mode === "finding" ? !!activeFinding : (!!intent.trim() || !!policyJson.trim()));

  const selectedCount = queue.reduce((n, f) => n + (selectedFindings.has(f.check_id) ? 1 : 0), 0);

  // Build the simulate request for a given finding (finding mode) or the current promote/deploy inputs.
  function buildReq(f: PolicyHandoffFinding | null): PolicySimulateReq {
    return {
      connection_id: connectionId || null,
      mode,
      intent: mode === "deploy" ? intent : "",
      policy_json: mode === "deploy" ? policyJson : "",
      assignment_id: mode === "promote" ? assignmentId : "",
      definition_id: mode === "promote" ? selected?.policy_definition_id ?? "" : "",
      current_effect: mode === "promote" ? selected?.effect ?? "" : "",
      current_enforcement: mode === "promote" ? selected?.enforcement_mode ?? "" : "",
      display_name: mode === "promote" ? selected?.display_name ?? "" : mode === "finding" ? f?.title ?? "" : "",
      non_compliant_resources: mode === "promote" ? selectedNc : -1,
      // finding-mode payload:
      check_id: mode === "finding" ? f?.check_id ?? "" : "",
      known_impact_count: mode === "finding" ? f?.flagged_count ?? -1 : -1,
      known_sample: mode === "finding" ? f?.flagged_resources ?? [] : [],
      frameworks: mode === "finding" ? f?.frameworks ?? {} : {},
      remediation: mode === "finding" ? f?.remediation ?? "" : "",
      resource_types: mode === "finding" ? f?.resource_types ?? [] : [],
      title: mode === "finding" ? f?.title ?? "" : "",
      workload_id: handoff?.workload_id || inv.workload?.id || "",
      scope,
      target_effect: mode === "finding" ? (f?.suggested_effect || effect) : effect,
      target_enforcement: enforcement,
    };
  }

  // Shared SSE step tracking driven off stepsRef (synchronous source of truth) and mirrored to
  // `steps` state for the progress UI. onComplete/onFail receive the result / error message.
  function trackingHandlers(onComplete: (r: PolicySimulateResult) => void, onFail: (m: string) => void) {
    const apply = (next: TrackedStep[]) => { stepsRef.current = next; setSteps(next); };
    const closeLast = (prev: TrackedStep[], ts: number) =>
      prev.map((p, i) => (i === prev.length - 1 && p.durationMs === undefined ? { ...p, durationMs: ts - p.startedAt } : p));
    return {
      onStatus: (s: PolicySimStatus) => {
        const ts = Date.now();
        setActiveKey(s.key);
        const prev = stepsRef.current;
        if (prev.length && prev[prev.length - 1].key === s.key) {
          // Same phase re-emitting (e.g. "Fetching…" → "Loaded") → update in place.
          const next = [...prev];
          next[next.length - 1] = { ...next[next.length - 1], message: s.message, detail: s.detail };
          apply(next);
        } else {
          // New phase → finalize the previous step's duration, then append.
          apply([...closeLast(prev, ts), { ...s, startedAt: ts }]);
        }
      },
      onDone: (r: PolicySimulateResult) => { apply(closeLast(stepsRef.current, Date.now())); setActiveKey(""); onComplete(r); },
      onError: (m: string) => { apply(closeLast(stepsRef.current, Date.now())); setActiveKey(""); onFail(m); },
    };
  }

  // Simulate a single finding (or the promote/deploy inputs) with live progress.
  async function simulateSingle(f: PolicyHandoffFinding | null) {
    const t0 = Date.now();
    setBatch(null); setBusy(true); setErr(""); setRes(null); setSteps([]); stepsRef.current = []; setActiveKey(""); setStartedAt(t0); setNow(t0); setViewingSavedAt("");
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    // Safety net: a hung AI phase can't keep the planner busy indefinitely.
    const timeout = window.setTimeout(() => ctrl.abort(), 180_000);
    try {
      await streamPolicySimulate(buildReq(f), trackingHandlers((r) => { setRes(r); setStep(4); void persistSimulation(r); }, setErr), ctrl.signal);
    } catch (e) {
      const aborted = (e as Error)?.name === "AbortError" || ctrl.signal.aborted;
      setErr(aborted ? "Simulation stopped." : (e as Error).message);
    } finally {
      window.clearTimeout(timeout);
      abortRef.current = null;
      setBusy(false);
    }
  }
  function run() { return simulateSingle(activeFinding); }
  // Stop an in-flight simulation (single or batch).
  function stopSim() { abortRef.current?.abort(); }

  // Simulate every selected finding in turn (sequential — each is a long live + AI call), keeping
  // per-finding results so they can all be reviewed and saved from one combined view.
  async function runBatch() {
    const chosen = queue.filter((f) => selectedFindings.has(f.check_id));
    if (chosen.length === 0) return;
    if (chosen.length === 1) { setActiveFindingIdx(queue.indexOf(chosen[0])); await simulateSingle(chosen[0]); return; }
    setRes(null); setErr(""); setViewingSavedAt("");
    setBatch(chosen.map((f) => ({ finding: f, status: "pending" as const })));
    setBusy(true);
    const t0 = Date.now(); setBatchStartedAt(t0); setNow(t0);
    for (let i = 0; i < chosen.length; i++) {
      if (abortRef.current?.signal.aborted) break; // Stop pressed mid-batch
      setBatchActive(i);
      setBatch((prev) => prev?.map((it, j) => (j === i ? { ...it, status: "running" } : it)) ?? null);
      const ts = Date.now(); setSteps([]); stepsRef.current = []; setActiveKey(""); setStartedAt(ts);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const timeout = window.setTimeout(() => ctrl.abort(), 180_000);
      // eslint-disable-next-line no-await-in-loop
      await new Promise<void>((resolve) => {
        streamPolicySimulate(buildReq(chosen[i]), trackingHandlers(
          (r) => { setBatch((prev) => prev?.map((it, j) => (j === i ? { ...it, status: "done", res: r, steps: stepsRef.current } : it)) ?? null); void persistSimulation(r); resolve(); },
          (m) => { setBatch((prev) => prev?.map((it, j) => (j === i ? { ...it, status: "error", err: m, steps: stepsRef.current } : it)) ?? null); resolve(); },
        ), ctrl.signal).catch((e) => {
          const aborted = (e as Error)?.name === "AbortError" || ctrl.signal.aborted;
          setBatch((prev) => prev?.map((it, j) => (j === i ? { ...it, status: "error", err: aborted ? "Stopped." : (e as Error).message } : it)) ?? null);
          resolve();
        });
      });
      window.clearTimeout(timeout);
    }
    abortRef.current = null;
    setBatchActive(-1); setBusy(false); setStep(4);
  }

  // Load a queued finding into the single-plan wizard.
  function loadFinding(i: number) {
    setActiveFindingIdx(i);
    setBatch(null); setRes(null); setStep(1); setSteps([]); setErr("");
    const f = queue[i];
    if (f?.suggested_effect) setEffect(f.suggested_effect);
  }

  function toggleFindingSel(id: string) {
    setSelectedFindings((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }

  // Save a finding's plan as a guardrail (write-back to the assessment bridge).
  async function savePlannedRes(r: PolicySimulateResult | null | undefined) {
    if (!r || !r.check_id) return;
    try {
      await api.policySaveEnforcementLink({
        workload_id: r.workload_id || handoff?.workload_id || inv.workload?.id || "",
        check_id: r.check_id,
        title: r.display_name,
        definition_id: r.builtin_match?.definition_id || "",
        builtin_name: r.builtin_match?.builtin_display_name || "",
        target_effect: r.target_state.effect,
        target_scope: r.target_state.scope_label || r.target_state.scope,
        go_no_go: r.plan?.go_no_go || "",
        plan_summary: r.plan?.summary || "",
        impact_count: r.impact.count,
        frameworks: r.frameworks || {},
      });
      setSavedChecks((p) => new Set(p).add(r.check_id!));
    } catch {
      /* ignore */
    }
  }
  const savePlanned = () => savePlannedRes(res);

  // Save every successfully-simulated finding in the batch as a guardrail.
  async function saveAllPlanned() {
    if (!batch) return;
    for (const it of batch) {
      if (it.status === "done" && it.res?.check_id && !savedChecks.has(it.res.check_id)) {
        // eslint-disable-next-line no-await-in-loop
        await savePlannedRes(it.res);
      }
    }
  }

  function reset() { setRes(null); setBatch(null); setBatchActive(-1); setStep(1); setErr(""); setSteps([]); stepsRef.current = []; setActiveKey(""); setStartedAt(0); setViewingSavedAt(""); }

  // Every completed simulation is auto-persisted so it can be reopened later (read-only history).
  async function persistSimulation(r: PolicySimulateResult) {
    try {
      await api.policySaveSimulation(r, wlId, inv.workload?.name || "", connectionId || "");
      qc.invalidateQueries({ queryKey: ["policySimulations"] });
    } catch {
      /* ignore */
    }
  }

  // Reopen a previously-saved simulation, showing its impact + staged plan exactly as it was.
  async function openSaved(id: string, createdAt: string) {
    try {
      const { simulation } = await api.policySimulation(id);
      setBatch(null); setErr(""); setSteps([]); stepsRef.current = []; setActiveKey("");
      setRes(simulation.result); setStep(4); setViewingSavedAt(createdAt);
    } catch {
      /* ignore */
    }
  }

  async function deleteSaved(id: string) {
    try {
      await api.policyDeleteSimulation(id);
      if (viewingSavedAt) reset();
      qc.invalidateQueries({ queryKey: ["policySimulations"] });
    } catch {
      /* ignore */
    }
  }
  const totalMs = (steps.reduce((a, s) => a + (s.durationMs ?? 0), 0)) || (startedAt ? (busy ? now : 0) - startedAt : 0);

  return (
    <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
      <div className="rounded-xl border bg-gradient-to-br from-brand/10 to-violet-50 p-4">
        <h2 className="flex items-center gap-2 text-base font-bold text-gray-800">🚦 AI Safe-Rollout Planner</h2>
        <p className="mt-0.5 text-xs text-gray-600">
          Simulate a policy change before you make it. Promote an existing policy (e.g. audit → deny) or deploy a new one,
          pick the target scope &amp; effect, and see the exact live impact plus a safe staged rollout plan. Read-only — nothing is applied.
        </p>
        <div className="mt-3 flex items-center gap-1 text-[11px]">
          {["Change", "Scope & mode", "Simulate", "Plan"].map((s, i) => (
            <div key={s} className="flex items-center gap-1">
              <span className={`flex h-5 w-5 items-center justify-center rounded-full font-bold ${step >= i + 1 ? "bg-brand text-white" : "bg-gray-200 text-gray-500"}`}>{i + 1}</span>
              <span className={step >= i + 1 ? "text-gray-700" : "text-gray-400"}>{s}</span>
              {i < 3 && <span className="mx-1 text-gray-300">→</span>}
            </div>
          ))}
        </div>
      </div>

      {/* Saved simulations — every run is persisted; reopen one to review its impact & plan. */}
      {savedSims.length > 0 && (
        <div className="rounded-xl border bg-white shadow-sm">
          <button onClick={() => setShowSaved((v) => !v)} className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left">
            <span className="flex items-center gap-2 text-sm font-semibold text-gray-800">
              📁 Saved simulations
              <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-500">{savedSims.length}</span>
            </span>
            <span className="text-xs text-gray-400">{showSaved ? "Hide" : "Show"}</span>
          </button>
          {showSaved && (
            <div className="max-h-72 space-y-1 overflow-y-auto border-t px-3 py-2">
              {savedSims.map((s) => (
                <div key={s.id} className={`flex items-center gap-2 rounded-lg border p-2 text-sm ${viewingSavedAt === s.created_at ? "border-brand bg-brand/5" : "border-gray-100 hover:bg-gray-50"}`}>
                  <button onClick={() => openSaved(s.id, s.created_at)} disabled={busy} className="flex min-w-0 flex-1 items-center justify-between gap-2 text-left disabled:opacity-50">
                    <span className="flex min-w-0 items-center gap-1">
                      <span className="truncate font-medium text-gray-800">{s.title}</span>
                      <span className="shrink-0 text-[11px] text-gray-500">· {s.scope_label || s.scope}</span>
                      {s.workload_name && !wlId && <span className="shrink-0 rounded bg-indigo-50 px-1 py-0.5 text-[9px] text-indigo-600">🧩 {s.workload_name}</span>}
                    </span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      <Pill cls={effectTone(s.target_effect)}>{s.target_effect}</Pill>
                      {s.impact_supported && <span className="text-[11px] text-gray-500"><b className="text-gray-700">{s.impact_count}</b> impacted</span>}
                      {s.go_no_go && <Pill cls={verdictTone(s.go_no_go)}>{s.go_no_go.toUpperCase()}</Pill>}
                      <span className="text-[10px] text-gray-400">{new Date(s.created_at).toLocaleString()}</span>
                    </span>
                  </button>
                  <button onClick={() => deleteSaved(s.id)} disabled={busy} title="Delete saved simulation" className="shrink-0 rounded p-1 text-gray-300 hover:bg-red-50 hover:text-red-500 disabled:opacity-50">🗑</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Step 1 — Change */}
      <Card title="1 · What are you changing?" icon="">
        <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
          <button onClick={() => { setMode("promote"); setBatch(null); }} className={`rounded-lg border p-2.5 text-left text-sm ${mode === "promote" ? "border-brand bg-brand/5" : "hover:bg-gray-50"}`}>
            <div className="font-medium text-gray-800">⬆️ Promote an existing policy</div>
            <div className="text-[11px] text-gray-500">Change the effect/enforcement of a live assignment (e.g. audit → deny).</div>
          </button>
          <button onClick={() => { setMode("deploy"); setBatch(null); }} className={`rounded-lg border p-2.5 text-left text-sm ${mode === "deploy" ? "border-brand bg-brand/5" : "hover:bg-gray-50"}`}>
            <div className="font-medium text-gray-800">✨ Deploy a new policy</div>
            <div className="text-[11px] text-gray-500">Describe it in natural language or paste policy JSON.</div>
          </button>
          <button onClick={() => { setMode("finding"); setBatch(null); }} className={`rounded-lg border p-2.5 text-left text-sm ${mode === "finding" ? "border-brand bg-brand/5" : "hover:bg-gray-50"}`}>
            <div className="font-medium text-gray-800">🛡️ From assessment{queue.length > 0 ? ` (${queue.length})` : ""}</div>
            <div className="text-[11px] text-gray-500">Enforce a failing assessment finding as a guardrail.</div>
          </button>
        </div>
        {mode === "finding" ? (
          queue.length === 0 ? (
            <div className="rounded-lg border border-dashed bg-gray-50/60 p-4 text-center text-xs text-gray-400">
              No findings handed over yet. Open an <b>assessment report</b>, select failing controls, and click
              “Plan enforcement in Azure Policy”.
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-gray-500">{queue.length} finding{queue.length === 1 ? "" : "s"} from the assessment — tick the ones to simulate together, or open one to plan it solo.</span>
                <button
                  onClick={() => setSelectedFindings((p) => (queue.every((f) => p.has(f.check_id)) ? new Set() : new Set(queue.map((f) => f.check_id))))}
                  className="text-[11px] font-medium text-brand hover:underline"
                >
                  {queue.every((f) => selectedFindings.has(f.check_id)) ? "Clear all" : "Select all"}
                </button>
              </div>
              <div className="space-y-1">
                {queue.map((f, i) => {
                  const sel = selectedFindings.has(f.check_id);
                  return (
                    <div
                      key={f.check_id}
                      className={`flex items-center gap-2 rounded-lg border p-2 text-sm ${i === activeFindingIdx ? "border-brand bg-brand/5" : sel ? "border-brand/40 bg-brand/[0.02]" : "border-gray-200 hover:bg-gray-50"}`}
                    >
                      <input
                        type="checkbox"
                        checked={sel}
                        onChange={() => toggleFindingSel(f.check_id)}
                        className="h-4 w-4 shrink-0 accent-brand"
                        title="Include in a combined simulation"
                      />
                      <button onClick={() => loadFinding(i)} className="flex min-w-0 flex-1 items-center justify-between gap-2 text-left">
                        <span className="min-w-0">
                          <span className="font-medium text-gray-800">{f.title}</span>
                          <span className="ml-1 text-[11px] text-gray-500">· {f.flagged_count} resource{f.flagged_count === 1 ? "" : "s"}</span>
                          {[...(f.frameworks.cis ?? []), ...(f.frameworks.nist ?? []).map((x: string) => `NIST ${x}`)].slice(0, 2).map((x) => (
                            <span key={x} className="ml-1 rounded bg-indigo-50 px-1 py-0.5 text-[9px] text-indigo-600">{x}</span>
                          ))}
                        </span>
                        <span className="flex shrink-0 items-center gap-1">
                          <Pill cls={f.severity === "error" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{f.severity}</Pill>
                          {savedChecks.has(f.check_id) && <span className="text-[10px] text-green-600">🛡 planned</span>}
                        </span>
                      </button>
                    </div>
                  );
                })}
              </div>
              {activeFinding && (
                <div className="rounded-lg bg-gray-50 p-2.5 text-xs text-gray-600">
                  <b>{activeFinding.title}</b> — {activeFinding.description}
                  <div className="mt-1 text-[11px] text-gray-500">Remediation: {activeFinding.remediation}</div>
                </div>
              )}
            </div>
          )
        ) : mode === "promote" ? (
          <div>
            <select value={assignmentId} onChange={(e) => { setAssignmentId(e.target.value); const a = inv.assignments.find((x) => x.id === e.target.value); if (a) { setScope(a.scope); if (a.effect) setEffect(a.effect.toLowerCase().includes("audit") ? "deny" : a.effect); } }} className="w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm">
              <option value="">Select an assignment to promote…</option>
              {inv.assignments.map((a) => <option key={a.id} value={a.id}>{a.display_name} — {a.effect || "?"} @ {a.scope_label}</option>)}
            </select>
            {selected && (
              <div className="mt-2 rounded-lg bg-gray-50 p-2.5 text-xs text-gray-600">
                Current: <Pill cls={effectTone(selected.effect)}>{selected.effect || "?"}</Pill> · {selected.enforcement_mode} @ {selected.scope_label}
                {selectedNc >= 0 ? <span className="ml-2 text-amber-700">· {selectedNc} non-compliant resource(s) today</span> : <span className="ml-2 text-gray-400">· run a compliance scan for exact impact</span>}
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            {tagDefs.length > 0 && (
              <div className="rounded-lg border border-brand/30 bg-brand/5 p-2.5">
                <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-gray-600">
                  <span className="font-medium text-brand">🏷️ From Tag Intelligence</span>
                  <span>· {tagDefs.length} generated definition{tagDefs.length > 1 ? "s" : ""} loaded. Pick one to simulate:</span>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {tagDefs.map((d, i) => (
                    <button key={d.name} onClick={() => loadTagDef(i)}
                      className={`flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] ${i === tagDefIdx ? "border-brand bg-white font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-white"}`}>
                      <Pill cls={effectTone(d.effect)}>{d.effect}</Pill>{d.tag}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <input value={intent} onChange={(e) => setIntent(e.target.value)} placeholder="Describe the policy: e.g. Deny storage accounts that allow public blob access" className="w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm" />
            <details className="text-xs" open={tagDefs.length > 0}><summary className="cursor-pointer text-gray-500">{tagDefs.length > 0 ? "Generated policy JSON (ready to simulate)" : "…or paste policy JSON"}</summary>
              <textarea value={policyJson} onChange={(e) => setPolicyJson(e.target.value)} placeholder='{"properties":{"policyRule":{...}}}' className="mt-1 h-24 w-full rounded-lg border border-gray-200 p-2 font-mono text-[11px]" />
            </details>
          </div>
        )}
      </Card>

      {/* Step 2 — Scope & target mode */}
      <Card title="2 · Target scope, effect & enforcement" icon="">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <label className="text-[11px] font-medium text-gray-500">Scope</label>
            <select value={scope} onChange={(e) => setScope(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm">
              <option value="">Select scope…</option>
              {inv.scope_tree.length > 0 && (
                <optgroup label="Scopes with assignments">
                  {inv.scope_tree.map((s) => <option key={s.scope} value={s.scope}>{"  ".repeat(s.depth)}{s.label}</option>)}
                </optgroup>
              )}
              {(() => {
                // All subscriptions (friendly names) so a rollout can target a subscription that
                // has no assignments yet. Skip ones already listed in the scope tree above.
                const inTree = new Set(inv.scope_tree.map((s) => s.scope.toLowerCase()));
                const subs = Object.entries(inv.subscription_names ?? {})
                  .map(([id, name]) => ({ scope: `/subscriptions/${id}`, name }))
                  .filter((s) => !inTree.has(s.scope.toLowerCase()))
                  .sort((a, b) => a.name.localeCompare(b.name));
                return subs.length > 0 ? (
                  <optgroup label="All subscriptions">
                    {subs.map((s) => <option key={s.scope} value={s.scope}>Sub: {s.name}</option>)}
                  </optgroup>
                ) : null;
              })()}
            </select>
            <input value={scope} onChange={(e) => setScope(e.target.value)} placeholder="…or paste a scope id" className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1 text-[11px]" />
          </div>
          <div>
            <label className="text-[11px] font-medium text-gray-500">Target effect</label>
            <select value={effect} onChange={(e) => setEffect(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm">
              {TARGET_EFFECTS.map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
            <div className="mt-1"><Pill cls={effectTone(effect)}>{effect}</Pill></div>
          </div>
          <div>
            <label className="text-[11px] font-medium text-gray-500">Enforcement</label>
            <select value={enforcement} onChange={(e) => setEnforcement(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm">
              <option value="Default">Default (enforce)</option>
              <option value="DoNotEnforce">DoNotEnforce (dry-run)</option>
            </select>
          </div>
        </div>
      </Card>

      {/* Step 3 — Run */}
      <div className="flex flex-wrap items-center gap-3">
        {mode === "finding" && selectedCount >= 2 ? (
          <>
            <button onClick={runBatch} disabled={!scope || busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
              {busy ? `Simulating ${batchActive >= 0 ? `${batchActive + 1}/${selectedCount}` : ""}… ${fmtDur(now - startedAt)}` : `🔮 Simulate ${selectedCount} findings together`}
            </button>
            {!busy && activeFinding && (
              <button onClick={run} disabled={!scope} className="rounded-lg border px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                or simulate only the highlighted one
              </button>
            )}
          </>
        ) : (
          <button onClick={run} disabled={!canRun || busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
            {busy ? `Simulating… ${fmtDur(now - startedAt)}` : res ? "🔄 Re-simulate" : "🔮 Simulate this change"}
          </button>
        )}
        {busy && (
          <button onClick={stopSim} className="rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50">
            ⏹ Stop
          </button>
        )}
        {(res || batch) && !busy && <button onClick={reset} className="rounded-lg border px-3 py-2 text-xs text-gray-600 hover:bg-gray-50">Start over</button>}
        {!scope && <span className="text-[11px] text-gray-400">Pick a target scope to enable.</span>}
      </div>
      {err && !batch && <ErrorBox message={err} />}

      {/* Batch (multi-select) results */}
      {batch && (
        <BatchResults
          batch={batch}
          batchActive={batchActive}
          busy={busy}
          steps={steps}
          activeKey={activeKey}
          now={now}
          batchStartedAt={batchStartedAt}
          savedChecks={savedChecks}
          onSaveOne={savePlannedRes}
          onSaveAll={saveAllPlanned}
        />
      )}

      {/* Live progress (single) */}
      {!batch && (busy || (steps.length > 0 && !res)) && <SimProgress steps={steps} activeKey={activeKey} busy={busy} now={now} />}

      {/* Step 4 — Result */}
      {!batch && res && (
        <>
          {viewingSavedAt && (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-brand/30 bg-brand/5 p-3 text-sm">
              <span className="font-medium text-gray-700">📁 Viewing a saved simulation from {new Date(viewingSavedAt).toLocaleString()}</span>
              <button onClick={reset} className="ml-auto rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">＋ New simulation</button>
            </div>
          )}
          {steps.length > 0 && (
            <details className="rounded-xl border bg-white px-4 py-2 text-xs shadow-sm">
              <summary className="cursor-pointer font-medium text-gray-600">✅ Simulation steps ({steps.length}) · took {fmtDur(totalMs)}</summary>
              <ol className="mt-2 space-y-1">
                {steps.map((s) => (
                  <li key={s.key + s.message} className="flex items-start gap-2 text-gray-600">
                    <span className="mt-0.5 text-green-500">✓</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-2"><span>{s.message}</span>{s.durationMs !== undefined && <span className="shrink-0 font-mono text-[10px] text-gray-400">{fmtDur(s.durationMs)}</span>}</div>
                      {s.detail && <div className="text-[11px] text-gray-400 break-words">{s.detail}</div>}
                    </div>
                  </li>
                ))}
              </ol>
            </details>
          )}
          <SimulationResult res={res} />
          {res.check_id && (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-white p-3 shadow-sm">
              {savedChecks.has(res.check_id) ? (
                <span className="text-sm font-medium text-green-700">🛡 Saved as a planned guardrail — the assessment report now shows it.</span>
              ) : (
                <button onClick={savePlanned} className="rounded-lg bg-green-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-green-700">
                  🛡 Save as planned guardrail
                </button>
              )}
              {activeFindingIdx < queue.length - 1 && (
                <button onClick={() => loadFinding(activeFindingIdx + 1)} className="rounded-lg border px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50">
                  Next finding ({activeFindingIdx + 2}/{queue.length}) →
                </button>
              )}
              <span className="ml-auto text-[11px] text-gray-400">Read-only — saving only records the plan; nothing is applied to Azure.</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function BatchResults({
  batch, batchActive, busy, steps, activeKey, now, batchStartedAt, savedChecks, onSaveOne, onSaveAll,
}: {
  batch: BatchItem[];
  batchActive: number;
  busy: boolean;
  steps: TrackedStep[];
  activeKey: string;
  now: number;
  batchStartedAt: number;
  savedChecks: Set<string>;
  onSaveOne: (r: PolicySimulateResult) => void;
  onSaveAll: () => void;
}) {
  const [open, setOpen] = useState<string>(""); // check_id of the expanded item
  const done = batch.filter((b) => b.status === "done").length;
  const errored = batch.filter((b) => b.status === "error").length;
  const totalImpact = batch.reduce((a, b) => a + (b.res?.impact.count ?? 0), 0);
  const verdicts = batch.reduce((acc, b) => {
    const g = (b.res?.plan?.go_no_go || "").toLowerCase();
    if (g) acc[g] = (acc[g] ?? 0) + 1;
    return acc;
  }, {} as Record<string, number>);
  const savable = batch.filter((b) => b.status === "done" && b.res?.check_id && !savedChecks.has(b.res.check_id!)).length;

  return (
    <div className="space-y-3">
      {/* Header / aggregate */}
      <div className="rounded-xl border bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-sm font-bold text-gray-800">
            {busy ? `🚦 Simulating ${batch.length} findings together…` : `✅ Simulated ${batch.length} findings`}
          </h3>
          {busy && batchActive >= 0 && (
            <span className="text-xs text-gray-500">Finding {batchActive + 1} of {batch.length} · {fmtDur(now - batchStartedAt)} elapsed</span>
          )}
          <span className="ml-auto flex items-center gap-2 text-[11px]">
            <span className="text-green-600">{done} done</span>
            {errored > 0 && <span className="text-red-600">· {errored} failed</span>}
            {busy && <span className="text-gray-400">· {batch.length - done - errored} queued</span>}
          </span>
        </div>
        {!busy && (
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
            <span className="rounded-lg bg-gray-50 px-2.5 py-1"><b className="text-gray-800">{totalImpact}</b> <span className="text-gray-500">resource(s) impacted across all</span></span>
            {Object.entries(verdicts).map(([k, v]) => (
              <span key={k} className="flex items-center gap-1"><Pill cls={verdictTone(k)}>{k.toUpperCase()}</Pill><span className="text-gray-400">×{v}</span></span>
            ))}
            {savable > 0 && (
              <button onClick={onSaveAll} className="ml-auto rounded-lg bg-green-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-green-700">
                🛡 Save all {savable} as guardrails
              </button>
            )}
            {savable === 0 && done > 0 && <span className="ml-auto text-[11px] font-medium text-green-700">🛡 All saved — the assessment report now shows them.</span>}
          </div>
        )}
        <p className="mt-2 text-[11px] text-gray-400">Read-only — each simulation only reads Azure and reasons with AI; nothing is applied.</p>
      </div>

      {/* Per-finding rows */}
      <div className="space-y-2">
        {batch.map((b, i) => {
          const isActive = busy && i === batchActive;
          const r = b.res;
          const isOpen = open === b.finding.check_id;
          return (
            <div key={b.finding.check_id} className={`rounded-xl border bg-white shadow-sm ${isActive ? "border-brand" : ""}`}>
              <div className="flex items-center gap-3 px-4 py-2.5">
                <span className="shrink-0 text-sm">
                  {b.status === "done" ? <span className="text-green-500">✓</span>
                    : b.status === "error" ? <span className="text-red-500">✗</span>
                    : b.status === "running" ? <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-brand align-middle" />
                    : <span className="text-gray-300">○</span>}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-gray-800">{b.finding.title}</div>
                  {r ? (
                    <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                      <Pill cls={effectTone(r.target_state.effect)}>{r.target_state.effect}</Pill>
                      <span><b className="text-gray-700">{r.impact.count}</b> impacted</span>
                      {r.plan?.go_no_go && <Pill cls={verdictTone(r.plan.go_no_go)}>{r.plan.go_no_go.toUpperCase()}</Pill>}
                      <span className="text-gray-400">{r.builtin_match?.matched ? "built-in match" : "custom policy"}</span>
                    </div>
                  ) : b.status === "error" ? (
                    <div className="mt-0.5 text-[11px] text-red-600">{b.err}</div>
                  ) : (
                    <div className="mt-0.5 text-[11px] text-gray-400">{isActive ? "Simulating…" : "Queued…"}</div>
                  )}
                </div>
                {r && (
                  <div className="flex shrink-0 items-center gap-2">
                    {r.check_id && (savedChecks.has(r.check_id)
                      ? <span className="text-[11px] font-medium text-green-700">🛡 planned</span>
                      : <button onClick={() => onSaveOne(r)} className="rounded-lg bg-green-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-green-700">🛡 Save</button>)}
                    <button onClick={() => setOpen(isOpen ? "" : b.finding.check_id)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">{isOpen ? "Hide" : "Details"}</button>
                  </div>
                )}
              </div>
              {isActive && (
                <div className="border-t px-4 py-3">
                  <SimProgress steps={steps} activeKey={activeKey} busy={busy} now={now} />
                </div>
              )}
              {isOpen && r && (
                <div className="border-t px-4 py-3">
                  <SimulationResult res={r} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SimProgress({ steps, activeKey, busy, now }: { steps: TrackedStep[]; activeKey: string; busy: boolean; now: number }) {
  const total = steps.reduce((a, s) => a + (s.durationMs ?? 0), 0) + (busy && steps.length ? Math.max(0, now - steps[steps.length - 1].startedAt) : 0);
  return (
    <Card title={`Running simulation… ${fmtDur(total)}`} icon="⏳" subtitle="Read-only — querying Azure and reasoning with AI. Nothing is applied.">
      <ol className="space-y-2">
        {steps.map((s) => {
          const active = busy && s.key === activeKey;
          const dur = s.durationMs !== undefined ? s.durationMs : active ? now - s.startedAt : 0;
          return (
            <li key={s.key} className="flex items-start gap-2.5 text-sm">
              <span className="mt-0.5 shrink-0">
                {active ? (
                  <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-brand" />
                ) : (
                  <span className="text-green-500">✓</span>
                )}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className={active ? "font-medium text-gray-800" : "text-gray-700"}>{s.message}</span>
                  <span className={`shrink-0 font-mono text-[10px] ${active ? "text-brand" : "text-gray-400"}`}>{fmtDur(dur)}</span>
                </div>
                {s.detail && <div className="mt-0.5 break-words font-mono text-[11px] text-gray-500">{s.detail}</div>}
              </div>
            </li>
          );
        })}
        {busy && (
          <li className="flex items-center gap-2.5 text-sm text-gray-400">
            <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-400" />
            {steps.length === 0 ? "Starting…" : "Working…"}
          </li>
        )}
      </ol>
    </Card>
  );
}

function SimulationResult({ res }: { res: PolicySimulateResult }) {
  const [copied, setCopied] = useState("");
  function copy(text: string, key: string) { navigator.clipboard?.writeText(text); setCopied(key); setTimeout(() => setCopied(""), 1500); }
  const impactVerb: Record<string, string> = {
    deny: "would be blocked on next create/update", denyaction: "would be blocked",
    deployifnotexists: "would trigger remediation deployments", modify: "would be mutated",
    audit: "would be flagged non-compliant", auditifnotexists: "would be flagged non-compliant",
  };
  const verb = impactVerb[(res.target_state.effect || "").toLowerCase()] ?? "would be affected";
  const go = res.plan?.go_no_go;

  return (
    <div className="space-y-4">
      {/* State transition */}
      <Card title="Change summary" icon="🔄">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium text-gray-800">{res.display_name}</span>
          {res.mode === "promote" && (
            <>
              <Pill cls={effectTone(res.current_state.effect)}>{res.current_state.effect || "?"}</Pill>
              <span className="text-gray-400">→</span>
            </>
          )}
          <Pill cls={effectTone(res.target_state.effect)}>{res.target_state.effect}</Pill>
          <span className="text-gray-500">· {res.target_state.enforcement} @ {res.target_state.scope_label || res.target_state.scope}</span>
        </div>
      </Card>

      {/* Impact */}
      <Card title="Live impact at this scope" icon="🎯">
        {!res.impact.supported ? (
          <div className="text-sm text-amber-600">{res.impact.message || "Impact couldn't be measured automatically."}</div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-3xl font-bold text-gray-800">{res.impact.count}</span>
              <span className="text-sm text-gray-600">resource(s) {verb}</span>
              {res.blast && <Pill cls={res.blast.risk_level === "low" ? "bg-green-100 text-green-700" : res.blast.risk_level === "high" || res.blast.risk_level === "critical" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{res.blast.risk_level} risk · {res.blast.risk_score}</Pill>}
              <Pill cls="bg-gray-100 text-gray-500">{res.impact.source === "compliance" ? "from Azure compliance" : res.impact.source === "resource_graph" ? "from Resource Graph" : "estimate"}</Pill>
            </div>
            {res.impact.message && <div className="text-[11px] text-gray-500">{res.impact.message}</div>}
            {res.blast && <div className="text-xs text-gray-600">{res.blast.summary} <b>{res.blast.recommendation}</b></div>}
            {res.impact.affected_resource_groups.length > 0 && (
              <div className="text-[11px] text-gray-500">Affected resource groups: {res.impact.affected_resource_groups.slice(0, 12).join(", ")}{res.impact.affected_resource_groups.length > 12 ? "…" : ""}</div>
            )}
            {res.impact.sample.length > 0 && (
              <details className="text-xs"><summary className="cursor-pointer text-gray-500">Sample resources ({res.impact.sample.length})</summary>
                <ul className="mt-1 space-y-0.5">{res.impact.sample.slice(0, 12).map((s) => <li key={s.id} className="truncate text-gray-600">{s.name} <span className="text-gray-400">· {s.type} · {s.resourceGroup}</span></li>)}</ul>
              </details>
            )}
          </div>
        )}
      </Card>

      {/* Plan */}
      {res.plan && (
        <Card title="Staged rollout plan" icon="🚦">
          <div className="flex items-center gap-2">
            {go && <Pill cls={go === "go" ? "bg-green-100 text-green-700" : go === "hold" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{go.toUpperCase()}</Pill>}
            <span className="text-sm text-gray-700">{res.plan.summary}</span>
          </div>
          {res.plan.rationale && <div className="mt-1 text-[11px] text-gray-500">{res.plan.rationale}</div>}
          {res.plan.impact_interpretation && <div className="mt-1 rounded bg-gray-50 px-2 py-1 text-xs text-gray-600">{res.plan.impact_interpretation}</div>}
          {res.plan.prerequisites?.length > 0 && (
            <div className="mt-2 text-xs"><span className="font-semibold text-violet-700">Prerequisites:</span> {res.plan.prerequisites.join(" · ")}</div>
          )}
          <ol className="mt-2 space-y-1">
            {res.plan.stages?.map((s, i) => (
              <li key={i} className="rounded-lg border p-2 text-xs">
                <span className="font-medium text-gray-800">{s.name}</span> <Pill cls="bg-gray-100 text-gray-600">{s.enforcement_mode}</Pill> <Pill cls={effectTone(s.effect)}>{s.effect}</Pill>
                {s.duration && <span className="ml-1 text-gray-400">· {s.duration}</span>}
                {s.selectors && <div className="text-gray-500">Selectors: {s.selectors}</div>}
                <div className="text-gray-500">Exit: {s.exit_criteria}</div>
              </li>
            ))}
          </ol>
          {res.plan.recommended_exemptions?.length > 0 && (
            <div className="mt-2 text-[11px] text-gray-600">
              <span className="font-semibold">Pre-seed exemptions:</span> {res.plan.recommended_exemptions.map((e) => `${e.scope} (${e.expires_in_days}d)`).join(", ")}
            </div>
          )}
          {res.plan.risks?.length > 0 && <div className="mt-1 text-[11px] text-amber-700">Risks: {res.plan.risks.join(" · ")}</div>}
        </Card>
      )}

      {/* Artifacts (copy-only) */}
      {(res.artifacts.assignment_json || res.artifacts.az_commands || res.artifacts.policy_definition) && (
        <Card title="Apply it yourself (copy-only — nothing is executed)" icon="📋">
          {res.artifacts.az_commands && res.artifacts.az_commands.length > 0 && (
            <div className="mb-2">
              <div className="mb-1 flex items-center gap-2"><span className="text-xs font-semibold text-gray-700">az CLI</span>
                <button onClick={() => copy(res.artifacts.az_commands!.join("\n"), "az")} className="rounded border px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50">{copied === "az" ? "Copied ✓" : "Copy"}</button>
              </div>
              <pre className="max-h-48 overflow-auto rounded bg-gray-900 p-2 font-mono text-[10px] text-gray-100">{res.artifacts.az_commands.join("\n")}</pre>
            </div>
          )}
          {res.artifacts.assignment_json && (
            <details className="text-xs"><summary className="cursor-pointer text-gray-500">Assignment JSON
              <button onClick={() => copy(JSON.stringify(res.artifacts.assignment_json, null, 2), "asg")} className="ml-2 rounded border px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50">{copied === "asg" ? "Copied ✓" : "Copy"}</button>
            </summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded bg-gray-900 p-2 font-mono text-[10px] text-gray-100">{JSON.stringify(res.artifacts.assignment_json, null, 2)}</pre>
            </details>
          )}
          {res.artifacts.policy_definition && (
            <details className="text-xs"><summary className="cursor-pointer text-gray-500">Policy definition JSON
              <button onClick={() => copy(JSON.stringify(res.artifacts.policy_definition, null, 2), "def")} className="ml-2 rounded border px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50">{copied === "def" ? "Copied ✓" : "Copy"}</button>
            </summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded bg-gray-900 p-2 font-mono text-[10px] text-gray-100">{JSON.stringify(res.artifacts.policy_definition, null, 2)}</pre>
            </details>
          )}
          {res.artifacts.aliases_used && res.artifacts.aliases_used.length > 0 && (
            <div className="mt-1 text-[11px] text-gray-500">Aliases: {res.artifacts.aliases_used.slice(0, 5).map((a) => <code key={a} className="mr-1">{a}</code>)}</div>
          )}
        </Card>
      )}
    </div>
  );
}

// =========================================================================== AI Tools
function AiTools({ inv, connectionId }: { inv: PolicyInventory; connectionId: string }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <WhatIfTool connectionId={connectionId} />
      <AuthorTool />
      <ExplainTool />
      <TriageTool assignments={inv.assignments} />
      <TagGovTool connectionId={connectionId} />
    </div>
  );
}

function WhatIfTool({ connectionId }: { connectionId: string }) {
  const [json, setJson] = useState("");
  const [res, setRes] = useState<PolicyWhatIf | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try { setRes(await api.policyWhatIf(json, connectionId || null)); } catch (e) { setRes({ supported: false, predicate: "", count: 0, sample: [], blast: null, message: (e as Error).message }); } finally { setBusy(false); }
  }
  return (
    <Card title="🔮 What-if impact simulation" icon="" subtitle="Paste a candidate deny policy; see how many live resources it would block.">
      <textarea value={json} onChange={(e) => setJson(e.target.value)} placeholder='{"properties":{"policyRule":{"if":{...},"then":{"effect":"deny"}}}}' className="h-28 w-full rounded-lg border border-gray-200 p-2 font-mono text-[11px]" />
      <button onClick={run} disabled={busy || !json.trim()} className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Simulating…" : "Simulate"}</button>
      {res && (
        <div className="mt-3 space-y-2 text-sm">
          {!res.supported ? <div className="text-amber-600">{res.message || "Couldn't translate this policy."}</div> : (
            <>
              <div className="flex items-center gap-2">
                <span className="text-2xl font-bold text-gray-800">{res.count}</span>
                <span className="text-gray-500">resources would be blocked today</span>
                {res.blast && <Pill cls={res.blast.risk_level === "low" ? "bg-green-100 text-green-700" : res.blast.risk_level === "high" || res.blast.risk_level === "critical" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{res.blast.risk_level} risk · {res.blast.risk_score}</Pill>}
              </div>
              {res.blast && <div className="text-xs text-gray-600">{res.blast.summary} <b>{res.blast.recommendation}</b></div>}
              {res.sample.length > 0 && (
                <details className="text-xs"><summary className="cursor-pointer text-gray-500">Sample ({res.sample.length})</summary>
                  <ul className="mt-1 space-y-0.5">{res.sample.slice(0, 12).map((s) => <li key={s.id} className="truncate text-gray-600">{s.name} <span className="text-gray-400">· {s.resourceGroup}</span></li>)}</ul>
                </details>
              )}
              <div className="font-mono text-[10px] text-gray-400">where {res.predicate}</div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function AuthorTool() {
  const [intent, setIntent] = useState("");
  const [res, setRes] = useState<PolicyAuthorResult | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() { setBusy(true); try { setRes(await api.policyAuthor(intent)); } finally { setBusy(false); } }
  return (
    <Card title="🗣️ Natural-language authoring" icon="" subtitle="Describe the rule; AI writes valid policy JSON with the right aliases.">
      <input value={intent} onChange={(e) => setIntent(e.target.value)} onKeyDown={(e) => e.key === "Enter" && intent.trim() && run()} placeholder='e.g. Deny public IPs on NICs except in the DMZ RG' className="w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm" />
      <button onClick={run} disabled={busy || !intent.trim()} className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Generating…" : "Generate policy"}</button>
      {res && (
        <div className="mt-3 space-y-2 text-sm">
          <div className="font-semibold text-gray-800">{res.display_name} <Pill cls={effectTone(res.recommended_effect)}>{res.recommended_effect}</Pill></div>
          <div className="text-xs text-gray-600">{res.description}</div>
          {res.aliases_used?.length > 0 && <div className="text-[11px] text-gray-500">Aliases: {res.aliases_used.slice(0, 4).map((a) => <code key={a} className="mr-1">{a}</code>)}</div>}
          {res.notes && <div className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">{res.notes}</div>}
          <details className="text-xs"><summary className="cursor-pointer text-gray-500">Policy JSON</summary>
            <pre className="mt-1 max-h-60 overflow-auto rounded bg-gray-900 p-2 font-mono text-[10px] text-gray-100">{JSON.stringify(res.policy_definition, null, 2)}</pre>
          </details>
        </div>
      )}
    </Card>
  );
}

function ExplainTool() {
  const [json, setJson] = useState("");
  const [res, setRes] = useState("");
  const [busy, setBusy] = useState(false);
  async function run() { setBusy(true); try { setRes((await api.policyExplain(json)).explanation); } finally { setBusy(false); } }
  return (
    <Card title="🔍 Explain this policy" icon="" subtitle="Paste any policy/initiative JSON for a plain-English breakdown.">
      <textarea value={json} onChange={(e) => setJson(e.target.value)} placeholder="Paste policy JSON…" className="h-24 w-full rounded-lg border border-gray-200 p-2 font-mono text-[11px]" />
      <button onClick={run} disabled={busy || !json.trim()} className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Explaining…" : "Explain"}</button>
      {res && <div className="mt-3 whitespace-pre-wrap text-xs leading-relaxed text-gray-700">{res}</div>}
    </Card>
  );
}

function TriageTool({ assignments }: { assignments: PolicyAssignment[] }) {
  const [err, setErr] = useState("");
  const [res, setRes] = useState<PolicyTriageResult | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() { setBusy(true); try { setRes(await api.policyTriage(err, assignments)); } finally { setBusy(false); } }
  return (
    <Card title="🚨 Deny-event triage" icon="" subtitle="Paste a deployment 'RequestDisallowedByPolicy' error; AI pinpoints the cause + fixes.">
      <textarea value={err} onChange={(e) => setErr(e.target.value)} placeholder="Paste the deployment error…" className="h-24 w-full rounded-lg border border-gray-200 p-2 font-mono text-[11px]" />
      <button onClick={run} disabled={busy || !err.trim()} className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Triaging…" : "Triage"}</button>
      {res && (
        <div className="mt-3 space-y-2 text-sm">
          <div><b>Likely policy:</b> {res.likely_policy}</div>
          <div className="text-xs text-gray-600">{res.explanation} <span className="text-gray-400">(property: {res.blocked_property})</span></div>
          <div className="space-y-1">
            {res.options?.map((o) => (
              <div key={o.action} className="rounded-lg border p-2 text-xs">
                <span className="font-medium text-gray-800">{o.action.replace(/_/g, " ")}</span>
                <Pill cls={o.risk === "low" ? "bg-green-100 text-green-700" : o.risk === "high" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}>{o.risk}</Pill>
                <div className="mt-0.5 text-gray-600">{o.summary}</div>
                <div className="text-gray-500">{o.steps}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function TagGovTool({ connectionId }: { connectionId: string }) {
  const [tags, setTags] = useState("owner, cost-center");
  const [res, setRes] = useState<PolicyTagGovernance | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try { setRes(await api.policyTagGovernance(tags.split(",").map((t) => t.trim()).filter(Boolean), connectionId || null)); } finally { setBusy(false); }
  }
  return (
    <Card title="🏷️ Tag-governance module" icon="" subtitle="Find resources missing required tags; AI proposes modify/inherit policies.">
      <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="owner, cost-center, environment" className="w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm" />
      <button onClick={run} disabled={busy || !tags.trim()} className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Scanning…" : "Find tag gaps"}</button>
      {res && (
        <div className="mt-3 space-y-2 text-sm">
          <div><span className="text-2xl font-bold text-gray-800">{res.missing_count}</span> <span className="text-gray-500">resources missing a required tag</span></div>
          {res.proposal?.summary && <div className="text-xs text-gray-600">{res.proposal.summary}</div>}
          {res.proposal?.modify_policies?.map((m) => (
            <div key={m.tag} className="text-[11px] text-gray-600">→ <b>{m.tag}</b>: {m.approach} ({m.policy_hint})</div>
          ))}
          {res.proposal?.audit_policy && <div className="text-[11px] text-gray-500">Audit with: {res.proposal.audit_policy}</div>}
        </div>
      )}
    </Card>
  );
}

// =========================================================================== Drift & IaC
function DriftIac({ inv }: { inv: PolicyInventory }) {
  const srcQ = useQuery({ queryKey: ["policyIacSource"], queryFn: api.policyIacSource, retry: false });
  const [content, setContent] = useState<string | null>(null);
  const [format, setFormat] = useState("epac");
  const [res, setRes] = useState<PolicyDriftResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const value = content ?? srcQ.data?.content ?? "";

  async function save() {
    setBusy(true);
    try { await api.policySetIacSource(value, format); setSaved(true); setTimeout(() => setSaved(false), 2000); } finally { setBusy(false); }
  }
  async function run() {
    setBusy(true);
    try { await api.policySetIacSource(value, format); setRes(await api.policyDrift(inv.assignments)); } finally { setBusy(false); }
  }

  return (
    <div className="space-y-4">
      <Card title="🔁 Policy-as-code drift" icon="" subtitle="Store your EPAC/Bicep/Terraform source of truth and diff it against live assignments.">
        <div className="mb-2 flex items-center gap-2">
          <select value={format} onChange={(e) => setFormat(e.target.value)} className="rounded-lg border border-gray-200 px-2 py-1 text-xs">
            <option value="epac">EPAC</option><option value="bicep">Bicep</option><option value="terraform">Terraform</option><option value="json">JSON</option>
          </select>
          <button onClick={save} disabled={busy} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">{saved ? "Saved ✓" : "Save source"}</button>
          <button onClick={run} disabled={busy} className="rounded-lg bg-brand px-3 py-1 text-xs font-medium text-white disabled:opacity-50">{busy ? "Analyzing…" : "Detect drift"}</button>
        </div>
        <textarea value={value} onChange={(e) => setContent(e.target.value)} placeholder="Paste your policy-as-code (assignments definitions)…" className="h-40 w-full rounded-lg border border-gray-200 p-2 font-mono text-[11px]" />
      </Card>
      {res && (
        <Card title={res.in_sync ? "✅ In sync" : "⚠️ Drift detected"} icon="">
          <div className="space-y-2 text-sm">
            {res.live_only?.length > 0 && (
              <div><div className="text-xs font-semibold text-red-700">Live only (portal drift)</div>
                {res.live_only.map((x, i) => <div key={i} className="text-[11px] text-gray-600">{x.name} <span className="text-gray-400">· {x.scope}</span> — {x.note}</div>)}
              </div>
            )}
            {res.code_only?.length > 0 && (
              <div><div className="text-xs font-semibold text-amber-700">Declared but not deployed</div>
                {res.code_only.map((x, i) => <div key={i} className="text-[11px] text-gray-600">{x.name} — {x.note}</div>)}
              </div>
            )}
            {res.mismatched?.length > 0 && (
              <div><div className="text-xs font-semibold text-violet-700">Mismatched</div>
                {res.mismatched.map((x, i) => <div key={i} className="text-[11px] text-gray-600">{x.name} — {x.difference}</div>)}
              </div>
            )}
            <div className="rounded bg-gray-50 px-2 py-1 text-xs text-gray-600">{res.recommendation}</div>
          </div>
        </Card>
      )}
    </div>
  );
}

// =========================================================================== History
function History({ connectionId }: { connectionId: string }) {
  const snapQ = useQuery({ queryKey: ["policySnapshots"], queryFn: api.policySnapshots, retry: false });
  const [busy, setBusy] = useState(false);
  const [drift, setDrift] = useState<Awaited<ReturnType<typeof api.policyTakeSnapshot>>["drift_since_previous"]>(null);
  async function snap() {
    setBusy(true);
    try { const r = await api.policyTakeSnapshot(connectionId || null, true); setDrift(r.drift_since_previous); snapQ.refetch(); } finally { setBusy(false); }
  }
  const snaps = snapQ.data?.snapshots ?? [];
  return (
    <div className="space-y-4">
      <Card title="🕑 Posture over time" icon="" subtitle="Capture point-in-time snapshots to track policy & compliance drift.">
        <button onClick={snap} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">{busy ? "Capturing…" : "📸 Take snapshot"}</button>
        {drift && (
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <DeltaPill label="Assignments" v={drift.assignments_delta} />
            <DeltaPill label="Exemptions" v={drift.exemptions_delta} />
            <DeltaPill label="Definitions" v={drift.definitions_delta} />
            <DeltaPill label="Non-compliant" v={drift.non_compliant_delta} invert />
          </div>
        )}
      </Card>
      <Card title={`Snapshots (${snaps.length})`} icon="">
        {snaps.length === 0 ? <Empty text="No snapshots yet." /> : (
          <Table head={["When", "By", "Assignments", "Exemptions", "Non-compliant"]}>
            {snaps.map((s) => (
              <tr key={s.id} className="border-t">
                <Td>{new Date(s.created_at).toLocaleString()}</Td>
                <Td className="text-gray-500">{s.created_by || "—"}</Td>
                <Td>{s.summary.counts.assignments ?? 0}</Td>
                <Td>{s.summary.counts.exemptions ?? 0}</Td>
                <Td>{s.summary.compliance.available ? s.summary.compliance.total_non_compliant_resources : "—"}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}

// =========================================================================== primitives
function Kpi({ label, value, sub, icon }: { label: string; value: number | string; sub?: string; icon: string }) {
  return (
    <div className="rounded-xl border bg-white p-3 shadow-sm">
      <div className="flex items-center justify-between"><span className="text-lg">{icon}</span></div>
      <div className="mt-1 text-2xl font-bold text-gray-800">{value}</div>
      <div className="text-[11px] text-gray-500">{label}</div>
      {sub && <div className="text-[10px] text-gray-400">{sub}</div>}
    </div>
  );
}

function HighlightCard({ tone, icon, title, value, total, desc }: { tone: string; icon: string; title: string; value: number; total?: number; desc: string }) {
  const ring = tone === "green" ? "border-green-200" : tone === "red" ? "border-red-200" : tone === "amber" ? "border-amber-200" : "border-violet-200";
  return (
    <div className={`rounded-xl border bg-white p-3 shadow-sm ${ring}`}>
      <div className="flex items-center gap-1.5 text-sm font-semibold text-gray-800"><span>{icon}</span>{title}</div>
      <div className="mt-1 text-2xl font-bold text-gray-800">{value}{total !== undefined && <span className="text-sm font-normal text-gray-400"> / {total}</span>}</div>
      <div className="text-[11px] text-gray-500">{desc}</div>
    </div>
  );
}

function Card({ title, icon, subtitle, children }: { title: string; icon: string; subtitle?: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm">
      <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-800">{icon && <span>{icon}</span>}{title}</h3>
      {subtitle && <p className="mb-2 mt-0.5 text-xs text-gray-500">{subtitle}</p>}
      <div className={subtitle ? "" : "mt-2"}>{children}</div>
    </div>
  );
}

function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <table className="w-full text-left text-sm">
      <thead className="bg-gray-50 text-[11px] uppercase text-gray-500">
        <tr>{head.map((h) => <th key={h} className="px-3 py-2 font-medium">{h}</th>)}</tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`px-3 py-2 align-top ${className}`}>{children}</td>;
}

function Pill({ cls, children }: { cls: string; children: ReactNode }) {
  return <span className={`ml-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>{children}</span>;
}

function DeltaPill({ label, v, invert }: { label: string; v: number; invert?: boolean }) {
  const good = invert ? v <= 0 : v >= 0;
  const cls = v === 0 ? "bg-gray-100 text-gray-500" : good ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700";
  return <span className={`rounded-full px-2 py-0.5 ${cls}`}>{label} {v >= 0 ? "+" : ""}{v}</span>;
}

function ScopeIcon({ kind }: { kind: string }) {
  // Map policy scope kinds to the proper Azure service glyphs (subscription = golden key,
  // resource group = dashed box, management group = stacked boxes). Tenant has no Azure glyph.
  if (kind === "subscription") return <AzureIcon kind="subscription" className="h-4 w-4" />;
  if (kind === "resourceGroup") return <AzureIcon kind="resource_group" className="h-4 w-4" />;
  if (kind === "managementGroup") return <AzureIcon kind="mg" className="h-4 w-4" />;
  if (kind === "tenant") return <span>🌐</span>;
  return <AzureIcon kind="subscription" className="h-4 w-4" />;
}

function Empty({ text }: { text: string }) {
  return <div className="rounded-lg border border-dashed bg-gray-50/60 p-4 text-center text-xs text-gray-400">{text}</div>;
}

function Loading({ text }: { text: string }) {
  return <div className="flex h-40 items-center justify-center text-sm text-gray-500">{text}</div>;
}

function ErrorBox({ message, soft }: { message: string; soft?: boolean }) {
  return (
    <div className={`mb-3 rounded-lg px-3 py-2 text-xs ${soft ? "bg-amber-50 text-amber-700" : "bg-red-50 text-red-700"}`}>
      {soft ? "⚠️ " : "❌ "}{message}
    </div>
  );
}
