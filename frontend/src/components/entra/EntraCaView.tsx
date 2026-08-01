import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type EntraBreakGlass,
  type EntraCaCell,
  type EntraCaCoverage,
  type EntraCaPolicy,
} from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import { ENTRA_CA_NAV, ENTRA_CA_TAB_IDS } from "../navConfig";
import {
  CA_STATE_RANK,
  CoverageBanner,
  EntraEmpty,
  SevBadge,
  SortTh,
  StateChip,
  cmp,
  useEntraSorted,
  useSortState,
  useSubTabRoute,
} from "./EntraShared";
import { EntraSimulatorView } from "./EntraSimulatorView";

/**
 * Conditional Access Command Center.
 *
 * The portal shows Conditional Access one policy at a time; every question that matters is a
 * join across policies. The coverage matrix is the headline artifact and the reason this
 * screen exists — so it states its own assumptions rather than presenting a number on trust.
 */

const CELL_STYLE: Record<string, { chip: string; label: string; title: string }> = {
  enforced: { chip: "bg-green-100 text-green-800", label: "✓", title: "Enforced for the whole cohort" },
  partial: { chip: "bg-amber-100 text-amber-800", label: "◐", title: "Enforced for part of the cohort" },
  report_only: { chip: "bg-sky-100 text-sky-700", label: "R", title: "Only a report-only policy applies — protects nobody" },
  none: { chip: "bg-red-100 text-red-700", label: "✕", title: "No enforced policy applies this control" },
};

// ------------------------------------------------------------------------- sorting
// Comparators live at module scope on purpose: one redefined on every render would be a new
// dependency each time and would defeat the memo inside `useEntraSorted`.
//
// Every grid here opens on a `natural` key — a comparator that says nothing, so the stable
// sort falls through to the order the server sent. The server's ordering for these grids is
// not reproducible from any single column (the break-glass list is the exception), so the
// alternative would be a first render that silently disagrees with the API.

type CoverageRow = EntraCaCoverage["matrix"][number];
type BreakGlassRow = EntraBreakGlass["candidates"][number];

/** Stable empty arrays: a fresh `[]` per render would re-run every sort memo. */
const NO_COVERAGE_ROWS: CoverageRow[] = [];
const NO_BREAK_GLASS_ROWS: BreakGlassRow[] = [];

type CoverageKey = "natural" | "cohort" | "size";

/**
 * The coverage matrix is not a list — it is a grid whose second axis is the control set.
 *
 * Only the row-identity columns (cohort, size) are sortable. The control columns are never
 * reordered: moving them would slide ✓/✕ cells out from under the label that explains them.
 * The application class is the *heading* of each table rather than a column, so rows are
 * sorted WITHIN each app-class block and the blocks themselves keep the server's order.
 */
function compareCoverageRow(a: CoverageRow, b: CoverageRow, key: CoverageKey): number {
  switch (key) {
    case "cohort": return cmp.text(a.label, b.label);
    case "size": return cmp.num(a.size, b.size);
    case "natural": return 0;
  }
}

type PolicyKey = "natural" | "policy" | "state" | "users" | "excluded" | "controls" | "apps";

function policyAppScope(p: EntraCaPolicy): string {
  return p.targets_all_apps ? "All cloud apps" : p.app_classes.join(", ");
}

function comparePolicy(a: EntraCaPolicy, b: EntraCaPolicy, key: PolicyKey): number {
  switch (key) {
    case "policy": return cmp.text(a.display_name, b.display_name);
    // `state` is an enum whose alphabet is meaningless: "disabled" would outrank "enabled".
    case "state": return cmp.rank(CA_STATE_RANK, a.state, b.state);
    case "users": return cmp.num(a.effective_user_count, b.effective_user_count);
    case "excluded": return cmp.num(a.excluded_user_count, b.excluded_user_count);
    // Both of these sort by the text the cell actually shows, so identical scopes group.
    case "controls": return cmp.text(a.controls.join(", "), b.controls.join(", "));
    case "apps": return cmp.text(policyAppScope(a), policyAppScope(b));
    case "natural": return 0;
  }
}

/**
 * Break-glass confirmation is tri-state, not a boolean.
 *
 * "Undecided" sits between the two settled answers so a descending sort leads with the
 * accounts an operator has vouched for, then the ones still awaiting a decision, and files
 * the explicitly rejected ones last. Sorting the words would put "confirmed" next to
 * "rejected" and call that an order.
 */
const BREAK_GLASS_CONFIRMED_RANK: Record<string, number> = {
  confirmed: 3, undecided: 2, rejected: 1,
};

function confirmedKey(v: boolean | null): string {
  return v === true ? "confirmed" : v === false ? "rejected" : "undecided";
}

type BreakGlassKey = "account" | "signals" | "risk" | "confirmed";

function compareBreakGlass(a: BreakGlassRow, b: BreakGlassRow, key: BreakGlassKey): number {
  switch (key) {
    case "account": return cmp.text(a.upn || a.display_name, b.upn || b.display_name);
    // The cell lists the heuristics that fired; `score` is what the server ranked them by,
    // so this column both reproduces the default order and restores it after a detour.
    case "signals": return cmp.num(a.score, b.score);
    case "risk": return cmp.bool(a.lockout_risk, b.lockout_risk);
    case "confirmed":
      return cmp.rank(BREAK_GLASS_CONFIRMED_RANK, confirmedKey(a.confirmed), confirmedKey(b.confirmed));
  }
}

export function EntraCaView({
  connectionId,
  onOpenSetup,
}: {
  connectionId: string | null;
  onOpenSetup: () => void;
}) {
  const [tab, setTab] = useSubTabRoute(ENTRA_CA_TAB_IDS, "coverage");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 border-b bg-white px-4">
        {ENTRA_CA_NAV.map((n) => (
          <button
            key={n.id}
            onClick={() => setTab(n.id)}
            className={`px-3 py-2 text-[13px] font-medium ${
              tab === n.id ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {n.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "coverage" && <CoverageTab connectionId={connectionId} onOpenSetup={onOpenSetup} />}
        {tab === "policies" && <PoliciesTab connectionId={connectionId} />}
        {tab === "conflicts" && <ConflictsTab connectionId={connectionId} />}
        {tab === "breakglass" && <BreakGlassTab connectionId={connectionId} />}
        {tab === "simulate" && <EntraSimulatorView connectionId={connectionId} />}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------- coverage
function CoverageTab({ connectionId, onOpenSetup }: { connectionId: string | null; onOpenSetup: () => void }) {
  const q = useQuery({
    queryKey: ["entra-ca-coverage", connectionId],
    queryFn: () => api.entraCaCoverage(connectionId),
  });
  const [cell, setCell] = useState<{ cohort: string; app_class: string; control: string } | null>(null);
  // One sort state for the whole matrix: the row axis is the same cohort list in every
  // app-class table, so a single ordering keeps the blocks comparable side by side.
  const [matrixSort, setMatrixSort] = useSortState<CoverageKey>(
    "ca-coverage-matrix", { key: "natural", dir: -1 },
  );
  const matrixRows = useEntraSorted(q.data?.matrix ?? NO_COVERAGE_ROWS, matrixSort, compareCoverageRow);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading coverage…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const data = q.data!;
  if (!data.meta.loaded) return <EntraEmpty kind="cold" />;
  const caDomain = data.meta.domains?.ca;
  if (caDomain && (caDomain.status === "blind" || caDomain.status === "unlicensed")) {
    return (
      <EntraEmpty
        kind={caDomain.status === "blind" ? "blind" : "unlicensed"}
        detail={caDomain.error || `Missing ${(caDomain.missing_permissions ?? []).join(", ")}`}
        onOpenSetup={onOpenSetup}
      />
    );
  }

  const h = data.headline;
  // "0 users × 0 applications are matched by no enforced policy" is a double negative that
  // reads as a failure at the exact moment the tenant is fully covered. State the good
  // outcome plainly instead.
  const fullyCovered = h.uncovered_users === 0 && h.uncovered_apps === 0;
  return (
    <div className="space-y-4 p-4">
      <CoverageBanner meta={data.meta} onOpenSetup={onOpenSetup} />

      {/* The sentence the whole page exists for. */}
      <div className="rounded-lg border bg-white p-4">
        {fullyCovered ? (
          <div className="text-lg font-semibold text-emerald-700">
            Every enabled user and application is matched by at least one enforced policy.
          </div>
        ) : (
          <div className="text-lg font-semibold text-gray-900">
            {h.uncovered_users.toLocaleString()} user{h.uncovered_users === 1 ? "" : "s"} ×{" "}
            {h.uncovered_apps.toLocaleString()} application{h.uncovered_apps === 1 ? "" : "s"} are matched by no
            enforced policy.
          </div>
        )}
        {h.privileged_uncovered > 0 && (
          <div className="mt-1 text-sm font-medium text-red-700">
            {h.privileged_uncovered} of those principals hold privileged roles.
          </div>
        )}
        <div className="mt-2 text-xs text-gray-500">
          Out of {h.total_users.toLocaleString()} enabled users and {h.total_apps.toLocaleString()} enterprise
          applications.
        </div>
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-gray-500">How this is counted</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-gray-500">
            {h.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </details>
        {h.privileged_uncovered_sample.length > 0 && (
          <div className="mt-2 text-xs text-gray-600">
            <span className="font-medium">Unprotected privileged principals: </span>
            {h.privileged_uncovered_sample.map((u) => u.name).join(", ")}
          </div>
        )}
      </div>

      {/* Cohort × application-class × control matrix. */}
      {data.app_classes.map((appClass) => (
        <div key={appClass.key} className="overflow-hidden rounded-lg border bg-white">
          <div className="border-b bg-gray-50 px-3 py-2 text-[13px] font-semibold text-gray-700">
            {appClass.label}
          </div>
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b bg-gray-50/50 text-left text-xs text-gray-500">
                <SortTh label="Cohort" col="cohort" sort={matrixSort} setSort={setMatrixSort}
                        firstDir={1} className="px-3" />
                <SortTh label="Size" col="size" sort={matrixSort} setSort={setMatrixSort}
                        className="px-2" />
                {/* Control columns are the other axis of the matrix and are never reordered. */}
                {data.controls.map((c) => (
                  <th key={c.key} className="px-2 py-1.5 text-center font-medium">
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrixRows.map((row) => (
                <tr key={row.cohort} className="border-b last:border-b-0">
                  <td className="px-3 py-1.5 text-gray-800">{row.label}</td>
                  <td className="px-2 py-1.5 text-gray-500">{row.size.toLocaleString()}</td>
                  {data.controls.map((c) => {
                    const key = `${appClass.key}|${c.key}`;
                    const cellData: EntraCaCell | undefined = row.cells[key];
                    const style = CELL_STYLE[cellData?.state ?? "none"];
                    return (
                      <td key={c.key} className="px-2 py-1.5 text-center">
                        <button
                          title={`${style.title}${cellData ? ` — ${cellData.covered}/${cellData.size}` : ""}`}
                          onClick={() => setCell({ cohort: row.cohort, app_class: appClass.key, control: c.key })}
                          disabled={!row.size}
                          className={`inline-flex h-6 min-w-[2.25rem] items-center justify-center rounded px-1 text-xs font-semibold ${
                            row.size ? style.chip : "bg-gray-50 text-gray-300"
                          }`}
                        >
                          {row.size ? style.label : "—"}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      <div className="flex flex-wrap gap-3 text-xs text-gray-500">
        {Object.entries(CELL_STYLE).map(([k, v]) => (
          <span key={k} className="inline-flex items-center gap-1">
            <span className={`inline-flex h-5 w-6 items-center justify-center rounded font-semibold ${v.chip}`}>
              {v.label}
            </span>
            {v.title}
          </span>
        ))}
      </div>

      {cell && <CellDrawer cell={cell} connectionId={connectionId} onClose={() => setCell(null)} />}
    </div>
  );
}

function CellDrawer({
  cell,
  connectionId,
  onClose,
}: {
  cell: { cohort: string; app_class: string; control: string };
  connectionId: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["entra-ca-cell", cell, connectionId],
    queryFn: () => api.entraCaCoverageCell(cell, connectionId),
  });
  return (
    <div className="fixed inset-y-0 right-0 z-30 w-[28rem] overflow-auto border-l bg-white shadow-xl">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="text-sm font-semibold">Coverage detail</div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-700">
          ✕
        </button>
      </div>
      <div className="p-4 text-[13px]">
        {q.isLoading && <div className="text-gray-500">Loading…</div>}
        {q.isError && <div className="text-red-600">{formatError(q.error)}</div>}
        {q.data && (
          <>
            <div className="text-gray-500">
              {q.data.cohort} · {cell.app_class} · {cell.control}
            </div>
            <div className="mt-2 font-medium">
              {q.data.cell.covered} of {q.data.cell.size} covered
            </div>
            {q.data.cell.policies.length > 0 && (
              <div className="mt-2">
                <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Policies applying</div>
                <ul className="mt-1 list-disc pl-5 text-gray-700">
                  {q.data.cell.policies.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="mt-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                Not covered ({q.data.cell.uncovered_total})
              </div>
              <ul className="mt-1 space-y-1">
                {q.data.uncovered.map((u) => (
                  <li key={u.id} className="flex items-center justify-between">
                    <span className="truncate text-gray-800">{u.name}</span>
                    {u.mfa_registered === false && (
                      <span className="ml-2 shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">
                        no MFA method
                      </span>
                    )}
                  </li>
                ))}
                {q.data.uncovered.length === 0 && <li className="text-gray-400">Everyone in this cohort is covered.</li>}
              </ul>
              {q.data.cell.uncovered_total > q.data.uncovered.length && (
                <div className="mt-1 text-xs text-gray-400">
                  Showing the first {q.data.uncovered.length} of {q.data.cell.uncovered_total}.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------- policies
function PoliciesTab({ connectionId }: { connectionId: string | null }) {
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 150);
  const q = useQuery({
    queryKey: ["entra-ca-policies", connectionId],
    queryFn: () => api.entraCaPolicies(connectionId),
  });
  const [selected, setSelected] = useState<string | null>(null);

  const rows = useMemo(() => {
    const all = q.data?.policies ?? [];
    if (!dSearch) return all;
    const needle = dSearch.toLowerCase();
    return all.filter((p) => p.display_name.toLowerCase().includes(needle));
  }, [q.data, dSearch]);
  // Filter first, then sort: the header reorders whatever the search box left behind.
  const [sort, setSort] = useSortState<PolicyKey>("ca-policies", { key: "natural", dir: -1 });
  const sorted = useEntraSorted(rows, sort, comparePolicy);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading policies…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  if (!q.data?.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!q.data.policies.length) return <EntraEmpty kind="clean" detail="This tenant has no Conditional Access policies." />;

  return (
    <div className="p-4">
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Filter policies…"
        className="mb-3 w-72 rounded border px-2 py-1.5 text-sm"
      />
      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
              <SortTh label="Policy" col="policy" sort={sort} setSort={setSort} firstDir={1} className="px-3" />
              <SortTh label="State" col="state" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by enforcement: enabled, then report-only, then disabled" />
              <SortTh label="Users" col="users" sort={sort} setSort={setSort} className="px-2" />
              <SortTh label="Excluded" col="excluded" sort={sort} setSort={setSort} className="px-2" />
              <SortTh label="Controls" col="controls" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Apps" col="apps" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr
                key={p.id}
                onClick={() => setSelected(p.id)}
                className="cursor-pointer border-b last:border-b-0 hover:bg-gray-50"
              >
                <td className="px-3 py-2 font-medium text-gray-800">{p.display_name}</td>
                <td className="px-2 py-2">
                  <PolicyStateChip policy={p} />
                </td>
                <td className="px-2 py-2 text-gray-600">{p.effective_user_count.toLocaleString()}</td>
                <td className="px-2 py-2 text-gray-600">{p.excluded_user_count.toLocaleString()}</td>
                <td className="px-2 py-2 text-gray-600">{p.controls.join(", ") || "—"}</td>
                <td className="px-2 py-2 text-gray-600">
                  {p.targets_all_apps ? "All cloud apps" : p.app_classes.join(", ") || "specific"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && <PolicyDrawer policyId={selected} connectionId={connectionId} onClose={() => setSelected(null)} />}
    </div>
  );
}

function PolicyStateChip({ policy }: { policy: EntraCaPolicy }) {
  if (policy.is_enforced) return <StateChip state="ok" title="Enabled and enforced" />;
  if (policy.is_report_only) return <StateChip state="partial" title="Report-only — protects nobody" />;
  return <StateChip state="not_collected" title="Disabled" />;
}

function PolicyDrawer({
  policyId,
  connectionId,
  onClose,
}: {
  policyId: string;
  connectionId: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["entra-ca-policy", policyId, connectionId],
    queryFn: () => api.entraCaPolicy(policyId, connectionId),
  });
  return (
    <div className="fixed inset-y-0 right-0 z-30 w-[30rem] overflow-auto border-l bg-white shadow-xl">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="text-sm font-semibold">Policy detail</div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-700">
          ✕
        </button>
      </div>
      <div className="space-y-3 p-4 text-[13px]">
        {q.isLoading && <div className="text-gray-500">Loading…</div>}
        {q.isError && <div className="text-red-600">{formatError(q.error)}</div>}
        {q.data && (
          <>
            <div className="text-base font-semibold text-gray-900">{q.data.policy.display_name}</div>
            <div className="flex flex-wrap gap-2">
              <PolicyStateChip policy={q.data.policy} />
              {q.data.policy.is_block && (
                <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">block</span>
              )}
              {q.data.policy.blocks_legacy && (
                <span className="rounded bg-green-100 px-1.5 py-0.5 text-[11px] text-green-700">blocks legacy auth</span>
              )}
            </div>
            <Field label="Effective users">{q.data.policy.effective_user_count.toLocaleString()}</Field>
            <Field label="Excluded users">{q.data.policy.excluded_user_count.toLocaleString()}</Field>
            <Field label="Grant controls">
              {q.data.policy.controls.join(", ") || "none"} ({q.data.policy.grant.operator})
              {q.data.policy.grant.auth_strength_name && ` · ${q.data.policy.grant.auth_strength_name}`}
            </Field>
            <Field label="Application scope">
              {q.data.policy.targets_all_apps ? "All cloud apps" : q.data.policy.app_classes.join(", ") || "specific apps"}
            </Field>
            <Field label="Last modified">{q.data.policy.modified_at || "unknown"}</Field>
            <Field label="Fingerprint">
              <code className="rounded bg-gray-100 px-1 text-xs">{q.data.policy.fingerprint}</code>
            </Field>
            {q.data.excluded_sample.length > 0 && (
              <div>
                <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Excluded (sample)</div>
                <ul className="mt-1 list-disc pl-5 text-gray-700">
                  {q.data.excluded_sample.map((u) => (
                    <li key={u.id}>{u.name}</li>
                  ))}
                </ul>
              </div>
            )}
            {q.data.conflicts.length > 0 && (
              <div>
                <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Conflicts</div>
                <ul className="mt-1 space-y-1">
                  {q.data.conflicts.map((c, i) => (
                    <li key={i} className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">
                      <span className="font-medium">{c.kind}</span> — {c.detail}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">{label}</div>
      <div className="text-gray-800">{children}</div>
    </div>
  );
}

// ------------------------------------------------------------------------ conflicts
const CONFLICT_LABEL: Record<string, { label: string; sev: string }> = {
  conflicting_block_grant: { label: "Block contradicts grant", sev: "high" },
  redundant_policy: { label: "Redundant", sev: "low" },
  duplicate_intent: { label: "Duplicate", sev: "low" },
  policy_no_effect: { label: "No effect", sev: "medium" },
  unreachable_condition: { label: "Unreachable condition", sev: "medium" },
  exclusion_privileged: { label: "Privileged exclusion", sev: "critical" },
  exclusion_sprawl: { label: "Exclusion sprawl", sev: "high" },
};

function ConflictsTab({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-ca-conflicts", connectionId],
    queryFn: () => api.entraCaConflicts(connectionId),
  });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Analysing policies…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  if (!q.data?.meta.loaded) return <EntraEmpty kind="cold" />;
  if (!q.data.conflicts.length)
    return (
      <EntraEmpty
        kind="clean"
        detail="No contradictions, shadowing, redundancy or ineffective policies were found."
        checked="block-vs-grant contradiction, shadowing, duplicate intent, empty scope, self-cancelling conditions, privileged exclusions, exclusion sprawl"
      />
    );

  return (
    <div className="space-y-2 p-4">
      {q.data.conflicts.map((c, i) => {
        const meta = CONFLICT_LABEL[c.kind] ?? { label: c.kind, sev: "medium" };
        return (
          <div key={i} className="rounded-lg border bg-white p-3">
            <div className="flex items-center gap-2">
              <SevBadge sev={meta.sev} />
              <span className="text-[13px] font-semibold text-gray-900">{meta.label}</span>
              <span className="text-[13px] text-gray-500">· {c.policy_name}</span>
            </div>
            <div className="mt-1 text-[13px] text-gray-700">{c.detail}</div>
            {/* The detail sentence already names the count for most kinds; repeating it
                underneath read like a rendering bug. The backend writes the number plain
                and this line groups it, so both spellings have to be checked or the
                de-duplication never fires. */}
            {c.affected > 0
              && !c.detail.includes(c.affected.toLocaleString())
              && !c.detail.includes(String(c.affected)) && (
              <div className="mt-1 text-xs text-gray-500">
                {c.affected.toLocaleString()} principal(s) affected.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------------- breakglass
function BreakGlassTab({ connectionId }: { connectionId: string | null }) {
  const q = useQuery({
    queryKey: ["entra-ca-breakglass", connectionId],
    queryFn: () => api.entraCaBreakGlass(connectionId),
  });
  const [busy, setBusy] = useState<string | null>(null);
  // The server ranks candidates by heuristic score; opening on that column reproduces it.
  const [sort, setSort] = useSortState<BreakGlassKey>("ca-breakglass", { key: "signals", dir: -1 });
  const candidates = useEntraSorted(q.data?.candidates ?? NO_BREAK_GLASS_ROWS, sort, compareBreakGlass);

  const confirm = async (userId: string, confirmed: boolean) => {
    setBusy(userId);
    try {
      await api.entraCaConfirmBreakGlass({ user_id: userId, confirmed }, connectionId);
      await q.refetch();
    } finally {
      setBusy(null);
    }
  };

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  if (!q.data?.meta.loaded) return <EntraEmpty kind="cold" />;

  return (
    <div className="space-y-3 p-4">
      <div className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-[13px] text-sky-900">
        {q.data.heuristic_note}
      </div>
      {q.data.over_covered.length > 0 && (
        <div className="rounded-lg border border-red-300 bg-red-50 p-3">
          <div className="text-[13px] font-semibold text-red-800">
            ⚠ {q.data.over_covered.length} confirmed emergency account(s) would be locked out
          </div>
          <div className="mt-1 text-xs text-red-700">
            These accounts are captured by an enforced policy whose control they cannot satisfy.
          </div>
        </div>
      )}
      {q.data.candidates.length === 0 ? (
        <EntraEmpty
          kind="clean"
          detail="No account looks like an emergency access account. If Conditional Access fails, there may be no way back in."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border bg-white">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
                <SortTh label="Account" col="account" sort={sort} setSort={setSort} firstDir={1} className="px-3" />
                <SortTh label="Signals" col="signals" sort={sort} setSort={setSort} className="px-2"
                        title="Sort by how strongly the account matches the break-glass heuristic" />
                <SortTh label="Risk" col="risk" sort={sort} setSort={setSort} className="px-2"
                        title="Sort by lockout risk" />
                <SortTh label="Confirmed" col="confirmed" sort={sort} setSort={setSort} className="px-2"
                        title="Sort by confirmation: confirmed, then undecided, then rejected" />
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <tr key={c.user_id} className="border-b last:border-b-0 align-top">
                  <td className="px-3 py-2">
                    <div className="font-medium text-gray-900">{c.upn || c.display_name}</div>
                    {c.is_global_admin && <div className="text-xs text-gray-500">Global Administrator</div>}
                  </td>
                  <td className="px-2 py-2 text-xs text-gray-600">
                    <ul className="list-disc pl-4">
                      {c.reasons.map((r) => (
                        <li key={r}>{r}</li>
                      ))}
                    </ul>
                  </td>
                  <td className="px-2 py-2">
                    {c.lockout_risk ? (
                      <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">
                        lockout risk
                      </span>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
                  </td>
                  <td className="px-2 py-2">
                    <div className="flex gap-1">
                      <button
                        disabled={busy === c.user_id}
                        onClick={() => confirm(c.user_id, true)}
                        className={`rounded px-2 py-1 text-xs font-medium ${
                          c.confirmed === true ? "bg-green-600 text-white" : "border text-gray-700 hover:bg-gray-50"
                        }`}
                      >
                        Yes
                      </button>
                      <button
                        disabled={busy === c.user_id}
                        onClick={() => confirm(c.user_id, false)}
                        className={`rounded px-2 py-1 text-xs font-medium ${
                          c.confirmed === false ? "bg-gray-700 text-white" : "border text-gray-700 hover:bg-gray-50"
                        }`}
                      >
                        No
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
