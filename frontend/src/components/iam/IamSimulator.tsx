/** Simulator tab — *"if I make this change, what actually happens?"*
 *
 * Three columns: **lost**, **retained anyway**, **gained**, with orphaned resources called out
 * above them. The middle column is the reason the screen exists — removing somebody from a group
 * frequently revokes nothing, because they hold the same role by another route, and a tool that
 * only reports removals encourages revocations that achieve nothing while leaving a false record
 * of remediation behind.
 *
 * A failed simulation is never a green tick. An invalid change is a 400 and a deleted referent is
 * a 409; both are surfaced with their message rather than collapsing into "no impact".
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type IamSimAccess, type IamSimulation } from "../../api";
import { useIamConnectionId } from "./IamShared";

const KIND_LABEL: Record<string, string> = {
  remove_assignment: "Remove an assignment",
  remove_group_member: "Remove someone from a group",
  remove_group: "Delete a group entirely",
  convert_to_eligible: "Convert standing access to PIM-eligible",
  rescope_assignment: "Narrow an assignment's scope",
  replace_role: "Swap one role for another",
  disable_bypass: "Disable a bypass credential",
  assume_principal: "Assume a principal is compromised",
  add_delegation: "Onboard a delegation",
};

const FIELDS: Record<string, string[]> = {
  remove_assignment: ["assignment_id"],
  remove_group_member: ["group_id", "principal_id"],
  remove_group: ["group_id"],
  convert_to_eligible: ["assignment_id"],
  rescope_assignment: ["assignment_id", "to_scope"],
  replace_role: ["assignment_id", "to_role"],
  disable_bypass: ["resource_id"],
  assume_principal: ["principal_id"],
  add_delegation: ["principal_id", "scope", "role_name"],
};

function Column({
  title,
  tone,
  hint,
  items,
}: {
  title: string;
  tone: string;
  hint: string;
  items: IamSimAccess[];
}) {
  return (
    <div className="min-w-0 flex-1">
      <div className={`rounded-t border-b-2 px-2 py-1 text-xs font-semibold ${tone}`}>
        {title} ({items.length})
      </div>
      <p className="px-2 py-1 text-[10px] text-gray-500">{hint}</p>
      <div className="space-y-1 px-1">
        {items.map((i, n) => (
          <div key={`${i.principalId}-${i.scope}-${n}`} className="rounded border bg-white px-2 py-1">
            <div className="truncate text-[11px] font-medium text-gray-800">{i.principalName || i.principalId}</div>
            <div className="truncate text-[10px] text-gray-600">{i.roleName} @ {i.scopeName || i.scope}</div>
            {i.otherPath && (
              <div className="truncate text-[10px] text-amber-800">
                still held via {i.otherPath}{i.otherVia ? ` (${i.otherVia})` : ""}
              </div>
            )}
          </div>
        ))}
        {items.length === 0 && <div className="px-1 py-1 text-[10px] text-gray-400">none</div>}
      </div>
    </div>
  );
}

function Results({ r }: { r: IamSimulation }) {
  return (
    <div className="space-y-3">
      {/* Above the columns on purpose: this is the outcome that gets a revocation reverted in a
          panic two weeks later, and it is knowable in advance. */}
      {r.orphaned_resources.length > 0 && (
        <div data-testid="orphaned-panel" className="rounded border border-red-300 bg-red-50 p-2">
          <div className="text-[11px] font-semibold text-red-900">
            {r.orphaned_resources.length} scope(s) would be left with no owner-level access
          </div>
          <ul className="mt-1 space-y-0.5">
            {r.orphaned_resources.map((o) => (
              <li key={o.resourceId} className="text-[11px] text-red-900">
                {o.resourceId}
                {o.hasRecordedOwner
                  ? " — an owner is recorded, so somebody can be asked"
                  : " — and nobody is recorded as its owner either"}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex gap-2">
        <Column
          title="Lost"
          tone="border-red-400 bg-red-50 text-red-900"
          hint="Access that genuinely disappears."
          items={r.access_lost}
        />
        <Column
          title="Retained anyway"
          tone="border-amber-400 bg-amber-50 text-amber-900"
          hint="Looks revoked, is not — held by another route. Revoking these achieves nothing."
          items={r.access_retained_via_other_path}
        />
        <Column
          title="Gained"
          tone="border-sky-400 bg-sky-50 text-sky-900"
          hint="Access this change creates."
          items={r.access_gained}
        />
      </div>

      <div className="flex flex-wrap gap-3 text-[11px] text-gray-600">
        <span>{r.principals_affected} principal(s) affected</span>
        <span>{r.unchanged} grant(s) unchanged</span>
        <span>
          standing privilege {r.standing_privilege_before} → {r.standing_privilege_after}
        </span>
        {/* Published so no chart can render without it. */}
        <span data-testid="sample-line">
          {r.sample.sampled
            ? `sampled ${r.sample.size} of ${r.sample.population} (seed ${r.sample.seed}, ${r.sample.always_full} privileged always kept)`
            : `showing all ${r.sample.population}`}
        </span>
      </div>

      <div data-testid="sim-limitations" className="rounded border border-amber-300 bg-amber-50 p-2">
        <div className="mb-1 text-[11px] font-semibold text-amber-900">What this model did not evaluate</div>
        <ul className="space-y-1">
          {r.limitations.map((l) => (
            <li key={l} className="text-[11px] text-amber-900">{l}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function SimulatorTab() {
  const connectionId = useIamConnectionId();
  const [kind, setKind] = useState("remove_assignment");
  const [values, setValues] = useState<Record<string, string>>({});
  const [basket, setBasket] = useState<Record<string, string>[]>([]);

  const run = useMutation({
    mutationFn: () => api.iamSimulate(basket, connectionId),
  });

  const add = () => {
    setBasket((b) => [...b, { kind, ...values }]);
    setValues({});
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        <div className="text-sm font-semibold text-gray-800">What-if simulator</div>
        <p className="mt-1 text-[11px] text-gray-600">
          Pure modeling over the last collected snapshot — no Azure call, no write. Build a basket
          of changes so the interactions between them are modelled together rather than one at a time.
        </p>

        <div className="mt-2 flex flex-wrap items-center gap-1">
          <select
            value={kind}
            onChange={(e) => { setKind(e.target.value); setValues({}); }}
            aria-label="Change kind"
            className="rounded border border-gray-300 px-1.5 py-1 text-xs"
          >
            {Object.entries(KIND_LABEL).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
          {(FIELDS[kind] ?? []).map((f) => (
            <input
              key={f}
              value={values[f] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f]: e.target.value }))}
              placeholder={f.replace(/_/g, " ")}
              aria-label={f}
              className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-xs"
            />
          ))}
          <button
            type="button"
            onClick={add}
            disabled={(FIELDS[kind] ?? []).some((f) => !values[f])}
            className="rounded border bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Add to basket
          </button>
        </div>

        {basket.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {basket.map((c, i) => (
              <span key={i} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-700">
                {KIND_LABEL[c.kind] ?? c.kind}
                <button
                  type="button"
                  onClick={() => setBasket((b) => b.filter((_, n) => n !== i))}
                  className="ml-1 text-gray-500 hover:text-red-700"
                  aria-label="Remove change"
                >
                  ×
                </button>
              </span>
            ))}
            <button
              type="button"
              onClick={() => run.mutate()}
              disabled={run.isPending}
              className="ml-auto rounded border bg-white px-2 py-1 text-xs text-gray-700 shadow-sm hover:bg-gray-50 disabled:opacity-50"
            >
              {run.isPending ? "Modeling…" : `Simulate ${basket.length} change(s)`}
            </button>
          </div>
        )}

        {/* An invalid change is an error, never a reassuring "no impact". */}
        {run.isError && (
          <div data-testid="sim-error" className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-[11px] text-red-900">
            This change could not be modelled: {(run.error as Error).message}. Nothing was
            simulated — this is not a result showing no impact.
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {run.data ? (
          <Results r={run.data} />
        ) : (
          <div className="rounded border bg-white p-3 text-xs text-gray-600">
            Add a change and simulate it. The middle column of the result — access that looks
            revoked but is still held by another route — is usually the answer people came for.
          </div>
        )}
      </div>
    </div>
  );
}
