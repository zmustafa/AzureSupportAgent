import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type EntraCrossPlaneRow, type EntraPimPolicy } from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import { PimReviewPanel } from "../PimReviewView";
import { EntraActivationsView } from "./EntraActivationsView";
import { InvestigateLink } from "./InvestigateLink";
import {
  Bar, EntraEmpty, SevBadge, SortScopeNote, SortTh, cmp, useEntraSorted,
  useSortState, useSubTabRoute,
} from "./EntraShared";

/**
 * Privileged Access Mission Control.
 *
 * One cockpit for every form of privilege, including the join no Microsoft surface shows:
 * a principal holding directory power AND Azure control-plane power at the same time.
 */

// The three PIM tabs are deliberately distinct and ordered settings -> events -> drift:
//   "pim"         the policy: what activation *should* require (approval, MFA, max duration)
//   "activations" the events: who actually elevated, when, and what they did with it
//   "jit-hygiene" the drift: privilege that was meant to be Just-In-Time and quietly went
//                 permanent, plus eligible roles nobody ever activates (the former
//                 /identity/pim screen)
type Tab = "overview" | "assignments" | "pim" | "activations" | "jit-hygiene" | "cross-plane";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "assignments", label: "Assignments" },
  { id: "pim", label: "PIM config" },
  { id: "activations", label: "Activations" },
  { id: "jit-hygiene", label: "JIT hygiene" },
  { id: "cross-plane", label: "Cross-plane" },
];

export function EntraPrivilegedView({
  connectionId,
  onOpenSetup,
}: {
  connectionId: string | null;
  onOpenSetup: () => void;
}) {
  const [tab, setTab] = useSubTabRoute(TABS.map((t) => t.id), "overview");
  return (
    // h-full, not flex-1: the parent is EntraView's plain scroll box, not a flex column, so
    // flex-1 resolves to nothing there and this root would grow to its full content height.
    // h-full resolves against that box's definite height and bounds the tabs below it.
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b bg-white px-4">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-[13px] font-medium ${
              tab === t.id ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {/* The native tabs are plain documents that scroll as one block. The re-parented panel
          pins its own header and scrolls only its body, so it needs a *bounded* box — given a
          scrolling one it grows to full content height and its header scrolls away. So the
          container clips and each tab opts into the scrolling it actually wants. */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === "jit-hygiene" ? (
          <div className="flex h-full min-h-0 flex-col">
            <PimReviewPanel connectionId={connectionId} />
          </div>
        ) : (
          <div className="h-full overflow-auto">
            {tab === "overview" && <Overview connectionId={connectionId} onOpenSetup={onOpenSetup} />}
            {tab === "assignments" && <Assignments connectionId={connectionId} />}
            {tab === "pim" && <PimConfig connectionId={connectionId} onOpenSetup={onOpenSetup} />}
            {tab === "activations" && <EntraActivationsView connectionId={connectionId} />}
            {tab === "cross-plane" && <CrossPlane connectionId={connectionId} />}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * A headline number.
 *
 * Clickable when the number has somewhere to go: every one of these counts is a slice of a
 * grid on another sub-tab, and reading "55 standing privileged" and then rebuilding that
 * filter by hand is the kind of small friction that stops people looking.
 */
function Kpi({ label, value, tone, note, onClick, title }: {
  label: string; value: number | string; tone?: string; note?: string;
  onClick?: () => void; title?: string;
}) {
  const body = (
    <>
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      {note && <div className="mt-0.5 text-[11px] text-gray-500">{note}</div>}
    </>
  );
  if (!onClick) return <div className="rounded-lg border bg-white p-3">{body}</div>;
  return (
    <button onClick={onClick} title={title || `Show ${label.toLowerCase()}`}
            className="rounded-lg border bg-white p-3 text-left transition hover:border-brand hover:bg-brand/5">
      {body}
    </button>
  );
}

function Overview({ connectionId, onOpenSetup }: { connectionId: string | null; onOpenSetup: () => void }) {
  const navigate = useNavigate();
  // Each tile is a saved query against another sub-tab. Filters travel in the URL, so the
  // destination is bookmarkable and survives a reload like every other Entra screen.
  const drill = (sub: string, query: Record<string, string> = {}) => {
    const qs = new URLSearchParams(query).toString();
    navigate(`/entra/privileged/${sub}${qs ? `?${qs}` : ""}`);
  };
  const q = useQuery({
    queryKey: ["entra-priv-overview", connectionId],
    queryFn: () => api.entraPrivilegedOverview(connectionId),
  });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  const c = d.counts;

  return (
    <div className="space-y-4 p-4">
      <div className="grid gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Kpi label="Global admins" value={c.global_admins ?? 0}
             tone={(c.global_admins ?? 0) > 5 || (c.global_admins ?? 0) < 2 ? "text-red-600" : undefined}
             title="Opens every Global Administrator assignment, standing and eligible"
             onClick={() => drill("assignments", { kind: "all", q: "Global Administrator" })} />
        {/* These two tiles count PRINCIPALS; the grid they open lists ASSIGNMENTS, and one
            principal can hold several. The row count is therefore expected to exceed the
            tile, which is why each title says what the destination actually shows. */}
        <Kpi label="Privileged principals" value={c.privileged_principals ?? 0}
             title="People and workload identities holding a privileged role. Opens every privileged assignment — one principal can hold several."
             onClick={() => drill("assignments", { kind: "all", privileged: "1" })} />
        <Kpi label="Standing privileged" value={c.standing_privileged ?? 0}
             tone={(c.standing_privileged ?? 0) > 0 ? "text-amber-600" : undefined}
             title="Privileged access that is always on rather than activated on demand. Opens every standing privileged assignment, including those inherited through a group."
             onClick={() => drill("assignments", { kind: "standing", privileged: "1" })} />
        <Kpi label="Eligible" value={c.eligible ?? 0}
             title="Opens the assignments that require activation through PIM"
             onClick={() => drill("assignments", { kind: "eligible" })} />
        <Kpi label="PIM fully configured" value={`${c.pim_fully_configured ?? 0}/${c.pim_policies ?? 0}`}
             tone={(c.pim_policies ?? 0) > 0 && (c.pim_fully_configured ?? 0) < (c.pim_policies ?? 0)
               ? "text-amber-600" : undefined}
             title="Opens the per-role PIM configuration health grid"
             onClick={() => drill("pim")} />
        <Kpi label="Cross-plane" value={c.cross_plane ?? 0}
             tone={(c.cross_plane ?? 0) > 0 ? "text-red-600" : undefined}
             title="Principals flagged for holding power in both Entra ID and Azure. Opens the full cross-plane grid, which lists every principal in the join."
             onClick={() => drill("cross-plane")} />
      </div>

      {!d.azure_link.available && (
        <div className="rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[13px] text-sky-900">
          <span className="font-medium">Cross-plane analysis unavailable.</span> {d.azure_link.reason}
        </div>
      )}
      {d.azure_link.available && d.azure_link.stale && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-900">
          The Azure RBAC cache ({d.azure_link.generated_at?.slice(0, 16) || "unknown"}) is older than this
          Entra snapshot. The cross-plane join is shown, but treat it as indicative rather than current.
        </div>
      )}

      <div className="rounded-lg border bg-white">
        <div className="border-b px-4 py-2 text-[13px] font-semibold text-gray-800">
          Privileged access findings
        </div>
        {d.findings.length === 0 ? (
          <EntraEmpty kind="clean" detail="No privileged-access findings." onOpenSetup={onOpenSetup} />
        ) : (
          <div className="divide-y">
            {d.findings.slice(0, 60).map((f) => (
              <div key={f.fingerprint} className="flex items-start gap-3 px-4 py-2">
                <SevBadge sev={f.severity} />
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] text-gray-900">{f.title}</div>
                  <div className="text-xs text-gray-500">{f.signal_id}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Assignment columns. These strings are the server's `sort` vocabulary, not just column ids —
 * the grid is capped at 2000 rows, so sorting in the browser would reorder the page the
 * server picked rather than the tenant. `""` means "no sort parameter", which is how the
 * server's own privileged-first default order is preserved on first load.
 */
type AssignKey = "principal" | "type" | "role" | "tier" | "kind" | "permanent" | "activation";

type AssignKind = "standing" | "eligible" | "all";
const ASSIGN_KINDS: AssignKind[] = ["standing", "eligible", "all"];

function Assignments({ connectionId }: { connectionId: string | null }) {
  // Filters live in the URL so a drill-through from the overview tiles is a real link:
  // bookmarkable, shareable, and unchanged by a reload. `standing` is the default and is
  // therefore left out of the address bar rather than written into it.
  const [params, setParams] = useSearchParams();
  const kindParam = params.get("kind") as AssignKind | null;
  const kind: AssignKind = kindParam && ASSIGN_KINDS.includes(kindParam) ? kindParam : "standing";
  const privilegedOnly = params.get("privileged") === "1";
  const urlSearch = params.get("q") ?? "";
  const [search, setSearch] = useState(urlSearch);
  const dSearch = useDebounced(search, 150);
  const [sort, setSort] = useSortState<AssignKey | "">("priv-assignments", { key: "", dir: -1 });

  const writeParams = (mutate: (next: URLSearchParams) => void) => {
    const next = new URLSearchParams(params);
    mutate(next);
    if (next.toString() !== params.toString()) setParams(next, { replace: true });
  };
  const setKind = (k: AssignKind) =>
    writeParams((next) => (k === "standing" ? next.delete("kind") : next.set("kind", k)));

  // A term arriving from a link (or the back button) has to reach the input.
  useEffect(() => { setSearch(urlSearch); }, [urlSearch]);
  // …and a term typed here has to reach the URL, once it has settled.
  useEffect(() => {
    writeParams((next) => (dSearch ? next.set("q", dSearch) : next.delete("q")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dSearch]);

  const q = useQuery({
    // The sort belongs in the key: a different order is a different server response, not a
    // re-render of this one.
    queryKey: ["entra-priv-assignments", connectionId, kind, privilegedOnly, dSearch, sort.key, sort.dir],
    queryFn: () => {
      const request = {
        kind,
        privileged: privilegedOnly || undefined,
        search: dSearch || undefined,
        sort: sort.key || undefined,
        dir: sort.key ? (sort.dir === 1 ? "asc" : "desc") : undefined,
      };
      return api.entraPrivilegedAssignments(request, connectionId);
    },
  });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-2">
        {(["standing", "eligible", "all"] as const).map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              kind === k ? "bg-gray-800 text-white" : "border text-gray-600"
            }`}
          >
            {k}
          </button>
        ))}
        {/* A filter arriving from a drill-through has to be visible and reversible, or the
            grid silently shows a subset and the reader has no way to know or undo it. */}
        {privilegedOnly && (
          <button
            onClick={() => writeParams((next) => next.delete("privileged"))}
            title="Show unprivileged role assignments as well"
            className="rounded-full border border-brand bg-brand/10 px-2 py-0.5 text-[11px] font-medium text-brand"
          >
            privileged roles only ✕
          </button>
        )}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by principal or role…"
          className="ml-auto w-72 rounded border px-2 py-1 text-sm"
        />
      </div>
      {!d.capabilities?.permanence_known && (
        <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          PIM schedule data was unavailable, so an active assignment cannot be distinguished from a
          live activation. Permanence is reported as unknown rather than assumed.
        </div>
      )}
      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
              <SortTh label="Principal" col="principal" sort={sort} setSort={setSort} firstDir={1}
                      className="px-3" title="Sort every matching assignment by principal" />
              <SortTh label="Type" col="type" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Role" col="role" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Tier" col="tier" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by role tier — tier 0 first" />
              <SortTh label="Kind" col="kind" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Permanent" col="permanent" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by permanence — unknown last" />
              <SortTh label="Last activation" col="activation" sort={sort} setSort={setSort}
                      className="px-2" title="Sort by last activation — never activated last" />
            </tr>
          </thead>
          <tbody>
            {d.assignments.map((a, i) => (
              <tr key={`${a.id}-${i}`} className="border-b last:border-b-0">
                <td className="px-3 py-1.5 text-gray-900">
                  {a.principal_name || a.principal_upn || a.principal_id}
                  {a.source === "group" && (
                    <span className="ml-1 text-xs text-gray-400">via {a.source_group_name}</span>
                  )}
                  <InvestigateLink principalId={a.principal_id}
                                   label={a.principal_name || a.principal_upn || a.principal_id} />
                </td>
                <td className="px-2 py-1.5 text-gray-600">{a.principal_type}</td>
                <td className="px-2 py-1.5 text-gray-800">{a.role_name}</td>
                <td className="px-2 py-1.5">
                  {a.role_tier === "tier0" ? (
                    <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">tier 0</span>
                  ) : a.role_tier === "tier1" ? (
                    <span className="rounded bg-orange-100 px-1.5 py-0.5 text-[11px] text-orange-700">tier 1</span>
                  ) : (
                    <span className="text-xs text-gray-400">tier 2</span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-gray-600">{a.assignment_kind}</td>
                <td className="px-2 py-1.5 text-gray-600">
                  {a.permanent === true ? "yes" : a.permanent === false ? "time-bound" : "unknown"}
                </td>
                <td className="px-2 py-1.5 text-gray-500">{a.last_activation?.slice(0, 10) || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <SortScopeNote shown={d.assignments.length} total={d.total} />
      </div>
      <div className="mt-2 text-xs text-gray-400">
        Showing {d.assignments.length} of {d.total}.
      </div>
    </div>
  );
}

type PimKey =
  | "role" | "score" | "mfa" | "approval" | "justification" | "ticket"
  | "duration" | "notifications" | "max_duration";

/**
 * Server control keys -> column keys. A control this build does not know about keeps a plain
 * header rather than offering a sort the comparator would silently ignore.
 */
const PIM_CONTROL_COL: Record<string, PimKey> = {
  mfa_on_activation: "mfa",
  approval_required: "approval",
  justification_required: "justification",
  ticket_required: "ticket",
  duration_bounded: "duration",
  notifications: "notifications",
};

/**
 * Every control cell is tri-state: satisfied, not satisfied, or never measured. `cmp.bool`
 * treats null as unknown and pins it to the bottom in both directions, which is the whole
 * point — a control we could not read is not a control that failed.
 */
function tri(v: boolean | null | undefined): boolean | null {
  return v == null ? null : Boolean(v);
}
/** Bounded-duration control. No recorded maximum is unknown, not "unbounded". */
function triHours(h: number | null | undefined, max = 8): boolean | null {
  return h == null ? null : h <= max;
}
function triCount(n: number | null | undefined): boolean | null {
  return n == null ? null : n > 0;
}
/** The MFA cell is the OR of two settings, so it is only unknown when neither was read. */
function triMfa(p: EntraPimPolicy): boolean | null {
  const mfa = tri(p.mfa_on_activation);
  const ctx = tri(p.auth_context_required);
  if (mfa === null && ctx === null) return null;
  return Boolean(mfa || ctx);
}

function comparePim(a: EntraPimPolicy, b: EntraPimPolicy, key: PimKey): number {
  switch (key) {
    case "role": return cmp.text(a.role_name, b.role_name);
    case "score": return cmp.num(a.score, b.score);
    case "mfa": return cmp.bool(triMfa(a), triMfa(b));
    case "approval": return cmp.bool(tri(a.approval_required), tri(b.approval_required));
    case "justification":
      return cmp.bool(tri(a.justification_required), tri(b.justification_required));
    case "ticket": return cmp.bool(tri(a.ticket_required), tri(b.ticket_required));
    case "duration":
      return cmp.bool(triHours(a.max_activation_hours), triHours(b.max_activation_hours));
    case "notifications":
      return cmp.bool(triCount(a.notification_recipients), triCount(b.notification_recipients));
    case "max_duration": return cmp.num(a.max_activation_hours, b.max_activation_hours);
  }
}

/** Stable identity for the pre-load render, so the sort memo is not rebuilt every frame. */
const NO_POLICIES: EntraPimPolicy[] = [];

function PimConfig({ connectionId, onOpenSetup }: { connectionId: string | null; onOpenSetup: () => void }) {
  const q = useQuery({
    queryKey: ["entra-priv-pim", connectionId],
    queryFn: () => api.entraPrivilegedPimPolicies(connectionId),
  });
  // Default is score ascending, which is the worst-configured-first order the server already
  // returns — the first render is unchanged.
  const [sort, setSort] = useSortState<PimKey>("priv-pim", { key: "score", dir: 1 });
  const policies = useEntraSorted(q.data?.policies ?? NO_POLICIES, sort, comparePim);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!d.policies.length) {
    const domain = d.domain;
    return (
      <EntraEmpty
        kind={domain?.status === "unlicensed" ? "unlicensed" : domain?.status === "blind" ? "blind" : "clean"}
        detail={
          domain?.error ||
          "No PIM role management policies were collected. This grid is the only place approval, " +
            "MFA-on-activation, justification and duration settings appear together."
        }
        onOpenSetup={onOpenSetup}
      />
    );
  }

  const cell = (ok: boolean) =>
    ok ? (
      <span className="text-green-600">✓</span>
    ) : (
      <span className="font-semibold text-red-600">✕</span>
    );

  return (
    <div className="p-4">
      <div className="mb-2 text-xs text-gray-500">
        Worst-configured privileged roles first. Every column is an activation control — what a
        user must do to turn the role on.
      </div>
      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
              <SortTh label="Role" col="role" sort={sort} setSort={setSort} firstDir={1} className="px-3" />
              <SortTh label="Score" col="score" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by configuration score" />
              {d.controls.map((c) => {
                const col = PIM_CONTROL_COL[c.key];
                return col ? (
                  <SortTh key={c.key} label={c.label} col={col} sort={sort} setSort={setSort}
                          align="center" className="px-2"
                          title={`Sort by ${c.label.toLowerCase()} — roles where it was never measured last`} />
                ) : (
                  <th key={c.key} className="px-2 py-2 text-center font-medium">{c.label}</th>
                );
              })}
              <SortTh label="Max duration" col="max_duration" sort={sort} setSort={setSort} className="px-2" />
            </tr>
          </thead>
          <tbody>
            {policies.map((p) => (
              <tr key={p.role_id} className="border-b last:border-b-0">
                <td className="px-3 py-1.5">
                  <span className="text-gray-900">{p.role_name}</span>
                  {p.role_tier === "tier0" && (
                    <span className="ml-1 rounded bg-red-100 px-1 py-0.5 text-[10px] text-red-700">tier 0</span>
                  )}
                </td>
                <td className="w-24 px-2 py-1.5">
                  <div className="flex items-center gap-1">
                    <Bar value={p.score} tone={p.score === 100 ? "bg-green-500" : p.score >= 60 ? "bg-amber-500" : "bg-red-500"} />
                    <span className="w-8 text-right text-xs text-gray-600">{p.score}</span>
                  </div>
                </td>
                <td className="px-2 py-1.5 text-center">{cell(p.mfa_on_activation || p.auth_context_required)}</td>
                <td className="px-2 py-1.5 text-center">{cell(p.approval_required)}</td>
                <td className="px-2 py-1.5 text-center">{cell(p.justification_required)}</td>
                <td className="px-2 py-1.5 text-center">{cell(p.ticket_required)}</td>
                <td className="px-2 py-1.5 text-center">
                  {cell(p.max_activation_hours != null && p.max_activation_hours <= 8)}
                </td>
                <td className="px-2 py-1.5 text-center">{cell(p.notification_recipients > 0)}</td>
                <td className="px-2 py-1.5 text-gray-600">
                  {p.max_activation_hours != null ? `${p.max_activation_hours}h` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}


type CrossKey = "principal" | "entra" | "azure" | "scope";

/** The scope cell, as a single string, so the column sorts by what the reader can see. */
function crossScopeText(r: EntraCrossPlaneRow): string {
  return r.azure_broad_scopes.join(", ") || r.azure_subscriptions.slice(0, 2).join(", ");
}

function compareCross(a: EntraCrossPlaneRow, b: EntraCrossPlaneRow, key: CrossKey): number {
  switch (key) {
    case "principal": return cmp.text(a.name, b.name);
    // Both power columns are lists; their length is the thing with an order.
    case "entra":
      return cmp.num(a.entra_roles.length + a.entra_permissions.length,
                     b.entra_roles.length + b.entra_permissions.length);
    case "azure": return cmp.num(a.azure_roles.length, b.azure_roles.length);
    case "scope": return cmp.text(crossScopeText(a), crossScopeText(b));
  }
}

const NO_CROSS_ROWS: EntraCrossPlaneRow[] = [];

function CrossPlane({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-priv-crossplane", connectionId],
    queryFn: () => api.entraPrivilegedCrossPlane(connectionId),
  });
  // The server orders by powerful-Azure-role count descending, then name; descending on
  // that count with the stable tie-break reproduces it exactly, so nothing moves on load.
  const [sort, setSort] = useSortState<CrossKey>("priv-cross-plane", { key: "azure", dir: -1 });
  const rows = useEntraSorted(q.data?.rows ?? NO_CROSS_ROWS, sort, compareCross);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  return (
    <div className="space-y-3 p-4">
      <div className="rounded-lg border bg-white p-3 text-[13px] text-gray-700">
        <span className="font-medium">Entra power beside Azure power.</span> A principal holding both is
        a single point of total compromise — and this correlation does not exist in any Microsoft surface.
      </div>
      {!d.azure_link.available ? (
        <EntraEmpty kind="blind" detail={d.azure_link.reason || "No Azure RBAC scan is available."} />
      ) : (
        <>
          {d.azure_link.stale && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Azure RBAC data is from {d.azure_link.generated_at?.slice(0, 16).replace("T", " ")}, older than
              this Entra snapshot. Re-run the RBAC scan for a current join.
            </div>
          )}
          <div className="overflow-hidden rounded-lg border bg-white">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
                  <SortTh label="Principal" col="principal" sort={sort} setSort={setSort}
                          firstDir={1} className="px-3" />
                  <SortTh label="Entra power" col="entra" sort={sort} setSort={setSort} className="px-2"
                          title="Sort by how many directory roles and permissions this principal holds" />
                  <SortTh label="Azure power" col="azure" sort={sort} setSort={setSort} className="px-2"
                          title="Sort by how many powerful Azure roles this principal holds" />
                  <SortTh label="Scope" col="scope" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.principal_id} className={`border-b last:border-b-0 ${r.both_planes ? "bg-red-50/40" : ""}`}>
                    <td className="px-3 py-1.5">
                      <span className="text-gray-900">{r.name}</span>
                      <span className="ml-1 text-xs text-gray-400">{r.kind}</span>
                    </td>
                    <td className="px-2 py-1.5 text-gray-700">
                      {[...r.entra_roles, ...r.entra_permissions].slice(0, 4).join(", ") || "—"}
                    </td>
                    <td className="px-2 py-1.5 text-gray-700">
                      {r.azure_roles.length ? (
                        <span className="font-medium text-red-700">{r.azure_roles.join(", ")}</span>
                      ) : r.azure_all_roles ? (
                        <span className="text-gray-500">{r.azure_all_roles} role(s)</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-gray-500">
                      {r.azure_broad_scopes.join(", ") || r.azure_subscriptions.slice(0, 2).join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <SortScopeNote shown={d.rows.length} total={d.total} />
          </div>
        </>
      )}
    </div>
  );
}
