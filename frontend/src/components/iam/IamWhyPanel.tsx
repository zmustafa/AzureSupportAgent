/** The "why" panel — the answer to *can this principal do this, and why?*
 *
 * The single most important rendering rule here: **`indeterminate` is its own state.** It is
 * returned whenever an unevaluated ABAC condition or an unresolved role definition sits in the
 * decision path, and showing it as a yes (or a no) throws away the one thing that makes the
 * answer trustworthy. It gets its own colour, its own icon, and wording that cannot be misread
 * as a verdict.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamAssignmentRef, type IamDecision, type IamVerdict } from "../../api";
import { useIamConnectionId } from "./IamShared";

const VERDICT: Record<IamVerdict, { label: string; box: string; icon: string }> = {
  allowed: { label: "Allowed", box: "border-emerald-300 bg-emerald-50 text-emerald-900", icon: "✔" },
  denied: { label: "Denied", box: "border-red-300 bg-red-50 text-red-900", icon: "⛔" },
  not_granted: { label: "Not granted", box: "border-gray-300 bg-gray-50 text-gray-700", icon: "—" },
  indeterminate: { label: "Cannot be determined", box: "border-amber-300 bg-amber-50 text-amber-900", icon: "?" },
};

// A short menu beats free text for the common questions; the field stays editable for the rest.
const COMMON_ACTIONS = [
  { label: "Delete a VM", action: "Microsoft.Compute/virtualMachines/delete" },
  { label: "Assign a role", action: "Microsoft.Authorization/roleAssignments/write" },
  { label: "Read blob data", action: "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read" },
  { label: "Read a key vault secret", action: "Microsoft.KeyVault/vaults/secrets/getSecret" },
  { label: "Delete a storage account", action: "Microsoft.Storage/storageAccounts/delete" },
];

function AssignmentLine({ a, note }: { a: IamAssignmentRef; note?: string }) {
  return (
    <li className="text-xs text-gray-700">
      <b>{a.roleName}</b> at <span className="text-gray-600">{a.scopeDisplayName || a.scope}</span>
      {a.sourceGroupName && <span className="text-gray-500"> · via {a.sourceGroupName}</span>}
      {a.matchedBy && a.matchedBy !== "*" && (
        <span className="text-gray-400"> · matched {a.matchedBy}</span>
      )}
      {note && <span className="text-amber-700"> · {note}</span>}
    </li>
  );
}

export function WhyPanel({
  principalId,
  principalName,
  scope,
  onClose,
}: {
  principalId: string;
  principalName: string;
  scope: string;
  onClose: () => void;
}) {
  const connectionId = useIamConnectionId();
  const [action, setAction] = useState(COMMON_ACTIONS[0].action);
  const [pending, setPending] = useState(COMMON_ACTIONS[0].action);

  const q = useQuery({
    queryKey: ["iam", "effective", principalId, scope, action, connectionId ?? ""],
    queryFn: () =>
      api.iamEffective({
        principal_id: principalId,
        scope,
        action,
        connection_id: connectionId,
      }),
    enabled: Boolean(principalId && scope && action),
    staleTime: 60 * 1000,
  });

  const d: IamDecision | undefined = q.data;
  const v = d ? VERDICT[d.verdict] : null;

  return (
    <div className="flex h-full min-h-0 w-[28rem] flex-col border-l bg-white">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span className="text-sm font-semibold text-gray-800">Why?</span>
        <span className="min-w-0 flex-1 truncate text-xs text-gray-500" title={principalName || principalId}>
          {principalName || principalId}
        </span>
        <button onClick={onClose} className="rounded px-1.5 text-gray-400 hover:bg-gray-100" aria-label="Close">
          ✕
        </button>
      </div>

      <div className="space-y-2 border-b px-3 py-2">
        <div className="truncate text-xs text-gray-500" title={scope}>
          at <b className="text-gray-700">{scope}</b>
        </div>
        <select
          value={COMMON_ACTIONS.some((c) => c.action === action) ? action : ""}
          onChange={(e) => {
            if (e.target.value) {
              setPending(e.target.value);
              setAction(e.target.value);
            }
          }}
          className="w-full rounded border border-gray-300 px-2 py-1 text-xs"
          aria-label="Common actions"
        >
          {COMMON_ACTIONS.map((c) => (
            <option key={c.action} value={c.action}>{c.label}</option>
          ))}
          <option value="">(custom action below)</option>
        </select>
        <div className="flex gap-1">
          <input
            value={pending}
            onChange={(e) => setPending(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && setAction(pending.trim())}
            placeholder="Microsoft.Provider/type/action"
            aria-label="Azure action"
            className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 font-mono text-[11px]"
          />
          <button
            onClick={() => setAction(pending.trim())}
            className="rounded border px-2 py-1 text-xs text-gray-700 hover:bg-gray-50"
          >
            Check
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {q.isLoading && <div className="text-sm text-gray-500">Evaluating…</div>}
        {q.isError && <div className="text-sm text-red-600">Could not evaluate this action.</div>}

        {d && v && (
          <>
            <div className={`rounded-lg border p-3 ${v.box}`}>
              <div className="flex items-baseline gap-2">
                <span className="text-lg">{v.icon}</span>
                <span className="text-sm font-semibold">{v.label}</span>
                <span className="ml-auto rounded bg-white/60 px-1.5 text-[10px] uppercase text-gray-600">
                  {d.plane} plane
                </span>
              </div>
              <div className="mt-1 text-xs">{d.reason}</div>
            </div>

            {d.decidedBy && (
              <div>
                <div className="mb-1 text-xs font-semibold text-gray-700">Deciding assignment</div>
                <ul className="space-y-0.5">
                  <AssignmentLine a={d.decidedBy} />
                </ul>
                <div className="mt-0.5 break-all font-mono text-[10px] text-gray-400">
                  {d.decidedBy.assignmentId}
                </div>
              </div>
            )}

            {d.viaGroups.length > 0 && (
              <div>
                <div className="mb-1 text-xs font-semibold text-gray-700">Received via group</div>
                <ul className="space-y-0.5 text-xs text-gray-700">
                  {d.viaGroups.map((g) => (
                    <li key={g.groupId}>{g.groupName}</li>
                  ))}
                </ul>
              </div>
            )}

            {d.denyingAssignments.length > 0 && (
              <div>
                <div className="mb-1 text-xs font-semibold text-red-700">
                  Deny assignments ({d.denyingAssignments.length})
                </div>
                <ul className="space-y-0.5">
                  {d.denyingAssignments.map((a) => (
                    <AssignmentLine key={a.assignmentId} a={a} />
                  ))}
                </ul>
              </div>
            )}

            {d.grantingAssignments.length > 1 && (
              <div>
                <div className="mb-1 text-xs font-semibold text-gray-700">
                  Other granting assignments ({d.grantingAssignments.length - 1})
                </div>
                <ul className="space-y-0.5">
                  {d.grantingAssignments.slice(1).map((a) => (
                    <AssignmentLine key={a.assignmentId} a={a} />
                  ))}
                </ul>
              </div>
            )}

            {/* Shown even on an "allowed" verdict: a notAction that excluded one role is exactly
                what a reader is looking for when they expected a different answer. */}
            {d.notActionExclusions.length > 0 && (
              <div>
                <div className="mb-1 text-xs font-semibold text-gray-700">Excluded by notActions</div>
                <ul className="space-y-0.5">
                  {d.notActionExclusions.map((a) => (
                    <AssignmentLine key={a.assignmentId} a={a} note={a.notAction} />
                  ))}
                </ul>
              </div>
            )}

            {d.conditionUnevaluated.length > 0 && (
              <div className="rounded border border-amber-300 bg-amber-50 p-2">
                <div className="mb-1 text-xs font-semibold text-amber-900">
                  {d.conditionUnevaluated.length} assignment(s) carry an ABAC condition
                </div>
                <div className="mb-1 text-[11px] text-amber-800">
                  Conditions are not evaluated here, so the answer can differ per resource.
                </div>
                <ul className="space-y-1">
                  {d.conditionUnevaluated.map((a) => (
                    <li key={a.assignmentId} className="text-[11px] text-amber-900">
                      <b>{a.roleName}</b> at {a.scopeDisplayName || a.scope}
                      <div className="break-all font-mono text-[10px] text-amber-700">{a.condition}</div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {d.unknownRoles.length > 0 && (
              <div className="rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-900">
                Role definition(s) not collected, so their permissions could not be checked:{" "}
                <b>{d.unknownRoles.join(", ")}</b>. Refresh the scope to resolve them.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
