/** Compare tab — *"what changed since the last scan, and who did it?"*
 *
 * Two claims on this screen are easy to get wrong and expensive when they are:
 *
 *  - **`available: false` is not an all-clear.** A tenant with one scan has nothing to compare
 *    against. Rendering an empty change list the same way as "we compared and nothing moved"
 *    tells a reader their estate is stable on the day they installed the product.
 *  - **`unknown` is not blank.** An unattributed change means the Activity Log did not carry a
 *    matching event — because the window rolled past it, or because the match was ambiguous and
 *    the backend refused to guess. Showing an empty actor column reads as "nobody did this".
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type IamChange } from "../../api";
import { useIamConnectionId } from "./IamShared";

const CLASS_LABEL: Record<string, string> = {
  added: "Added",
  removed: "Removed",
  escalated: "Escalated",
  de_escalated: "De-escalated",
  re_scoped: "Re-scoped",
  activated: "Activated",
  deactivated: "Deactivated",
  path_changed: "Path changed",
  orphaned: "Orphaned",
};

const CLASS_CLASS: Record<string, string> = {
  added: "bg-red-100 text-red-800",
  escalated: "bg-red-100 text-red-800",
  re_scoped: "bg-orange-100 text-orange-800",
  activated: "bg-orange-100 text-orange-800",
  orphaned: "bg-amber-100 text-amber-800",
  removed: "bg-emerald-100 text-emerald-800",
  de_escalated: "bg-emerald-100 text-emerald-800",
  deactivated: "bg-emerald-100 text-emerald-800",
  path_changed: "bg-sky-100 text-sky-800",
};

function Actor({ change }: { change: IamChange }) {
  const a = change.actor;
  // Blind is not nobody. An unmatched change says so in words.
  if (!a || a.confidence === "unknown") {
    return (
      <span className="text-[11px] text-amber-800" title="No Activity Log event matched this change, or the match was ambiguous and was refused rather than guessed.">
        unknown actor
      </span>
    );
  }
  return (
    <span className="text-[11px] text-gray-700">
      {a.actorDisplayName || a.actorPrincipalId}
      {a.changeSource && a.changeSource !== "Unknown" && (
        <span className="ml-1 rounded bg-gray-100 px-1 text-[10px] text-gray-600">{a.changeSource}</span>
      )}
      {a.confidence === "inferred" && (
        <span className="ml-1 text-[10px] text-amber-700" title="Matched on scope and time rather than on the assignment id.">
          inferred
        </span>
      )}
    </span>
  );
}

function Row({ c }: { c: IamChange }) {
  return (
    <div className="flex items-baseline gap-2 border-b px-3 py-1.5 last:border-b-0">
      <span className={`shrink-0 rounded px-1.5 text-[10px] font-semibold uppercase ${CLASS_CLASS[c.class] ?? "bg-gray-100 text-gray-700"}`}>
        {CLASS_LABEL[c.class] ?? c.class}
      </span>
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800">
        {c.principalName || c.principalId}
        {c.privileged && <span className="ml-1 rounded bg-red-50 px-1 text-[10px] text-red-700">privileged</span>}
      </span>
      <span className="min-w-0 flex-1 truncate text-[11px] text-gray-600" title={c.scope}>
        {c.from && c.to && c.from.roleName !== c.to.roleName
          ? `${c.from.roleName} → ${c.to.roleName}`
          : c.roleName}
        {c.broader && <span className="ml-1 text-red-700">· broader scope</span>}
      </span>
      <span className="shrink-0"><Actor change={c} /></span>
    </div>
  );
}

export function CompareTab() {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [cls, setCls] = useState("");
  const [worseningOnly, setWorseningOnly] = useState(false);

  const q = useQuery({
    queryKey: ["iam", "diff", connectionId, cls],
    queryFn: () => api.iamDiff({ class: cls, connection_id: connectionId, limit: 500 }),
  });
  const attribute = useMutation({
    mutationFn: () => api.iamAttribute(30, connectionId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["iam", "diff"] }),
  });

  const d = q.data;
  const changes = useMemo(
    () => (d?.changes ?? []).filter((c) => !worseningOnly || c.worsens),
    [d, worseningOnly],
  );
  // `{}` is truthy in JavaScript, so testing the object alone rendered "0 exact, 0 inferred,
  // 0 unknown over the last 30 days" for a tenant where attribution had NEVER been run. That
  // reads as "we looked and nothing is unattributed", which is the opposite of the truth.
  const attributed = d?.attribution?.days ? d.attribution : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <span className="text-sm font-semibold text-gray-800">Access changes</span>
          {/* "0 changes" next to a banner saying the comparison could not be made is the exact
              reassuring zero this screen exists to prevent — a reader scanning the header takes
              it as "we compared and nothing moved". */}
          {d && !d.available ? (
            <span className="text-xs text-amber-800">not comparable</span>
          ) : d ? (
            <span className="text-xs text-gray-600">
              {d.total} change{d.total === 1 ? "" : "s"}
              {typeof d.worsening === "number" && d.worsening > 0 && (
                <span className="text-red-700"> · {d.worsening} increase risk</span>
              )}
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => attribute.mutate()}
            disabled={attribute.isPending || !d?.changes?.length}
            className="ml-auto rounded border bg-white px-2 py-1 text-xs text-gray-700 shadow-sm hover:bg-gray-50 disabled:opacity-50"
            title="Join these changes to the Azure Activity Log to find out who made them. Runs per subscription and is slow."
          >
            {attribute.isPending ? "Attributing…" : "Find out who"}
          </button>
        </div>

        {/* Nothing to compare against is NOT an all-clear. */}
        {d && !d.available && (
          <div data-testid="diff-unavailable" className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-900">
            There is no earlier snapshot to compare against, so no change can be shown. This is not
            a clean bill of health — it means the comparison could not be made.
            {d.note && <div className="mt-1">{d.note}</div>}
          </div>
        )}

        {attributed ? (
          <div className="mt-2 text-[11px] text-gray-600">
            Attribution: {attributed.attributed_exact ?? 0} exact, {attributed.attributed_inferred ?? 0} inferred,{" "}
            <b className="text-amber-800">{attributed.unattributed ?? 0} unknown</b> over the last {attributed.days ?? 30} days.
            {attributed.note && <span className="ml-1 text-amber-800">{attributed.note}</span>}
          </div>
        ) : (
          d?.available && changes.length > 0 && (
            <div className="mt-2 text-[11px] text-amber-800">
              Nobody has been attributed to these changes yet — “Find out who” has not been run,
              so every actor below is unknown for that reason rather than because no event exists.
            </div>
          )
        )}
      </div>

      <div className="flex items-center gap-2 border-b bg-gray-50 px-4 py-1.5">
        <select
          value={cls}
          onChange={(e) => setCls(e.target.value)}
          aria-label="Change class"
          className="rounded border border-gray-300 px-1.5 py-0.5 text-xs"
        >
          <option value="">All change types</option>
          {(d?.classes ?? Object.keys(CLASS_LABEL)).map((c) => (
            <option key={c} value={c}>{CLASS_LABEL[c] ?? c}</option>
          ))}
        </select>
        <label className="flex items-center gap-1 text-xs text-gray-700">
          <input type="checkbox" checked={worseningOnly} onChange={(e) => setWorseningOnly(e.target.checked)} />
          Only changes that increase risk
        </label>
        <span className="ml-auto text-[11px] text-gray-500">
          {d && !d.available ? "—" : `${changes.length} shown`}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {q.isLoading && <div className="p-4 text-sm text-gray-500">Loading…</div>}
        {d?.available && changes.length === 0 && (
          <div className="m-3 rounded border bg-white p-3 text-xs text-gray-600">
            Nothing changed between these two snapshots.
          </div>
        )}
        {d?.truncated && (
          <div className="border-b bg-amber-50 px-3 py-1 text-[11px] text-amber-900">
            Showing the first {d.changes.length} of {d.total} changes.
          </div>
        )}
        <div className="bg-white">
          {changes.map((c) => (
            <Row key={`${c.class}-${c.key}`} c={c} />
          ))}
        </div>
      </div>
    </div>
  );
}
