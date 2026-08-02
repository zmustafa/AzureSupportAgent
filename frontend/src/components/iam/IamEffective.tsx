/** Effective Access tab — the action-level evaluator, as a screen rather than a row popover.
 *
 *  This is the question the product exists to answer: *can this principal perform this action
 *  on this scope, and why?* The engine has been here since P4, but the only way to reach it was
 *  a small button in the last column of a virtualized grid, and the two inverse pivots
 *  (`/iam/resource-access`, `/iam/principal/{id}/access`) had no caller at all.
 *
 *  Three directions, because a real access review asks all three:
 *    · **can**   — one principal, one action, one scope → a verdict and its evidence chain
 *    · **who**   — one action, one scope → everyone who can do it (each one re-evaluated, so a
 *                  principal blocked by a deny assignment does not appear)
 *    · **reach** — one principal → every role they hold at or above a scope
 *
 *  The rendering rule inherited from the Why panel and enforced by sharing its components:
 *  `indeterminate` is its own state. It is never merged into the allowed list, never rounded to
 *  a yes or a no, and it is counted separately in every headline.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamAssignmentRef, type IamScopeNode } from "../../api";
import { useIamConnectionId } from "./IamShared";
import { ActionPicker, COMMON_ACTIONS, DecisionResult, VERDICT } from "./IamWhyPanel";

type Mode = "can" | "who" | "reach";

const MODES: { id: Mode; label: string; hint: string }[] = [
  { id: "can", label: "Can this principal…", hint: "One principal, one action, one scope — the verdict and the assignment that decided it." },
  { id: "who", label: "Who can…", hint: "Everyone who can perform this action here. Every candidate is re-evaluated, so a deny assignment removes them." },
  { id: "reach", label: "What can they reach", hint: "Every role this principal holds at or above the scope, split by plane." },
];

/** The tree's root is a synthetic "All scopes" node whose id is the EMPTY STRING — a
 *  no-filter sentinel for the access grid, not an ARM scope. Offering it here produces a
 *  question ARM cannot answer ("who can delete a VM at *all scopes*"), and, worse, selecting it
 *  leaves `scope` falsy, which disables the query and renders **nothing at all** — no verdict,
 *  no error, no "pick a scope". Blind reading as clean, again. It is dropped, and its children
 *  are lifted to depth 0. */
function flattenScopes(node: IamScopeNode | undefined, depth = 0): { id: string; label: string }[] {
  if (!node) return [];
  const kids = node.children.flatMap((c) => flattenScopes(c, node.id ? depth + 1 : depth));
  if (!node.id) return kids;
  return [{ id: node.id, label: `${"— ".repeat(depth)}${node.name}` }, ...kids];
}

function RefList({ title, refs, tone = "text-gray-700" }: { title: string; refs: IamAssignmentRef[]; tone?: string }) {
  if (refs.length === 0) return null;
  return (
    <div>
      <div className={`mb-1 text-xs font-semibold ${tone}`}>{title} ({refs.length})</div>
      <ul className="space-y-0.5">
        {refs.map((a, i) => (
          <li key={`${a.assignmentId}-${i}`} className="text-xs text-gray-700">
            <b>{a.roleName}</b> at <span className="text-gray-600">{a.scopeDisplayName || a.scope}</span>
            {a.sourceGroupName && <span className="text-gray-500"> · via {a.sourceGroupName}</span>}
            {typeof a.actionCount === "number" && (
              <span className="text-gray-400"> · {a.actionCount} action{a.actionCount === 1 ? "" : "s"}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One row of the "who can" answer. The verdict travels with the name — a list of people under
 *  a single heading invites the reader to assume they are all the same kind of yes. */
function WhoRow({ p }: { p: { principalId: string; principalName: string; verdict: string; decidedBy: IamAssignmentRef | null; reason: string } }) {
  const v = VERDICT[p.verdict as keyof typeof VERDICT] ?? VERDICT.indeterminate;
  return (
    <div className="flex items-start gap-2 border-b px-3 py-1.5 last:border-0">
      <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${v.box}`}>{v.icon} {v.label}</span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-gray-800" title={p.principalId}>
          {p.principalName || p.principalId}
        </div>
        {p.decidedBy && (
          <div className="truncate text-[11px] text-gray-500">
            {p.decidedBy.roleName} at {p.decidedBy.scopeDisplayName || p.decidedBy.scope}
            {p.decidedBy.sourceGroupName ? ` · via ${p.decidedBy.sourceGroupName}` : ""}
          </div>
        )}
        <div className="text-[11px] text-gray-500">{p.reason}</div>
      </div>
    </div>
  );
}

export function EffectiveTab() {
  const connectionId = useIamConnectionId();
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const [mode, setMode] = useState<Mode>((params.get("mode") as Mode) || "can");
  const [principalId, setPrincipalId] = useState(params.get("principal_id") || "");
  const [scope, setScope] = useState(params.get("scope") || "");
  const [action, setAction] = useState(params.get("action") || COMMON_ACTIONS[0].action);

  const dirQ = useQuery({
    queryKey: ["iam", "roles", connectionId ?? ""],
    queryFn: () => api.iamRoles(connectionId),
    staleTime: 5 * 60 * 1000,
  });
  const treeQ = useQuery({
    queryKey: ["iam", "scope-tree", connectionId ?? ""],
    queryFn: () => api.iamScopeTree(connectionId),
    staleTime: 5 * 60 * 1000,
  });

  const principals = (dirQ.data?.principals ?? []) as Record<string, unknown>[];
  const scopeOptions = useMemo(() => flattenScopes(treeQ.data?.root), [treeQ.data]);

  // Default the scope to the tree root once it loads, but never overwrite one that arrived in
  // the URL — a deep link from the grid's Why panel names the scope the reader was looking at.
  useEffect(() => {
    if (!scope && scopeOptions.length) setScope(scopeOptions[0].id);
  }, [scope, scopeOptions]);

  const canQ = useQuery({
    queryKey: ["iam", "effective", principalId, scope, action, connectionId ?? ""],
    queryFn: () => api.iamEffective({ principal_id: principalId, scope, action, connection_id: connectionId }),
    enabled: mode === "can" && Boolean(principalId && scope && action),
    staleTime: 60 * 1000,
  });
  const whoQ = useQuery({
    queryKey: ["iam", "who-can", scope, action, connectionId ?? ""],
    queryFn: () => api.iamResourceAccess({ scope, action, connection_id: connectionId }),
    enabled: mode === "who" && Boolean(scope && action),
    staleTime: 60 * 1000,
  });
  const reachQ = useQuery({
    queryKey: ["iam", "principal-access", principalId, scope, connectionId ?? ""],
    queryFn: () => api.iamPrincipalAccess(principalId, scope || "/", connectionId),
    enabled: mode === "reach" && Boolean(principalId),
    staleTime: 60 * 1000,
  });

  const needsPrincipal = mode !== "who";
  const needsAction = mode !== "reach";
  const activeMode = MODES.find((m) => m.id === mode)!;
  // A scope is mandatory for two of the three modes, and an unset one disables the query — so
  // it has to be SAID, not left as an empty results column that reads like an answer.
  const noScope = !scope && mode !== "reach";
  const noScopesAtAll = !treeQ.isLoading && scopeOptions.length === 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => setMode(m.id)}
            className={`rounded border px-2 py-1 text-xs ${mode === m.id ? "border-brand bg-brand/10 font-medium text-brand" : "border-gray-300 bg-white text-gray-600 hover:bg-gray-50"}`}
          >
            {m.label}
          </button>
        ))}
        <span className="ml-2 text-[11px] text-gray-500">{activeMode.hint}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
          <div className="space-y-3 rounded-lg border bg-white p-3">
            {needsPrincipal && (
              <label className="block">
                <span className="text-xs font-semibold text-gray-700">Principal</span>
                <input
                  list="iam-principal-options"
                  value={principalId}
                  onChange={(e) => setPrincipalId(e.target.value.trim())}
                  placeholder="Object id (GUID)"
                  aria-label="Principal"
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono text-[11px]"
                />
                <datalist id="iam-principal-options">
                  {principals.slice(0, 2000).map((p) => (
                    <option key={String(p.principalId)} value={String(p.principalId)}>
                      {String(p.displayName ?? "")} {p.principalType ? `(${String(p.principalType)})` : ""}
                    </option>
                  ))}
                </datalist>
                {/* Naming the size of the list matters: an empty picker on a tenant that was
                    never scanned looks identical to a tenant with no principals. */}
                <span className="mt-0.5 block text-[10px] text-gray-400">
                  {dirQ.isLoading ? "loading directory…" : `${principals.length} principal(s) in the cached directory`}
                </span>
              </label>
            )}

            <label className="block">
              <span className="text-xs font-semibold text-gray-700">Scope</span>
              <select
                value={scopeOptions.some((s) => s.id === scope) ? scope : ""}
                onChange={(e) => e.target.value && setScope(e.target.value)}
                aria-label="Scope"
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1 text-xs"
              >
                {scopeOptions.map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
                <option value="">(resource id below)</option>
              </select>
              <input
                value={scope}
                onChange={(e) => setScope(e.target.value.trim())}
                placeholder="/subscriptions/…/resourceGroups/…/providers/…"
                aria-label="Scope id"
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono text-[11px]"
              />
              {noScopesAtAll && (
                <span className="mt-0.5 block text-[10px] text-amber-700">
                  No management group or subscription in the cached scan — type a resource id, or run an access scan.
                </span>
              )}
            </label>

            {needsAction && (
              <div className="space-y-2">
                <span className="text-xs font-semibold text-gray-700">Action</span>
                <ActionPicker value={action} onChange={setAction} />
              </div>
            )}
          </div>

          <div className="space-y-3">
            {noScope && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
                Pick a scope. Nothing is evaluated until one is chosen — an empty results panel is not an answer.
              </div>
            )}

            {mode === "can" && !noScope && (
              <>
                {!principalId && <div className="rounded-lg border bg-white p-4 text-sm text-gray-600">Pick a principal to evaluate.</div>}
                {canQ.isLoading && <div className="text-sm text-gray-500">Evaluating…</div>}
                {canQ.isError && <div className="text-sm text-red-600">Could not evaluate this action.</div>}
                {canQ.data && <DecisionResult d={canQ.data} />}
              </>
            )}

            {mode === "who" && !noScope && (
              <>
                {whoQ.isLoading && <div className="text-sm text-gray-500">Evaluating every candidate…</div>}
                {whoQ.isError && <div className="text-sm text-red-600">Could not evaluate this scope.</div>}
                {whoQ.data && (
                  <>
                    <div className="rounded-lg border bg-white p-3">
                      <div className="text-sm text-gray-800">
                        <b className="tabular-nums">{whoQ.data.allowed.length}</b> of{" "}
                        <b className="tabular-nums">{whoQ.data.candidates}</b> principal(s) with any grant at or above
                        this scope can perform <span className="font-mono text-[11px]">{whoQ.data.action}</span>
                        {whoQ.data.indeterminate.length > 0 && (
                          <>
                            {" "}· <b className="tabular-nums text-amber-700">{whoQ.data.indeterminate.length}</b> could not be determined
                          </>
                        )}
                        .
                      </div>
                      <div className="mt-0.5 text-[11px] text-gray-500">{whoQ.data.plane} plane</div>
                      {/* A never-scanned tenant answers "nobody", which is the most reassuring
                          possible rendering of "we have no data". Say which one it is. */}
                      {whoQ.data.candidates === 0 && (
                        <div className="mt-2 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-900">
                          No principal holds any grant at or above this scope in the cached scan. That is not the
                          same as nobody being able to do this — run an access scan if you expected results.
                        </div>
                      )}
                    </div>

                    {whoQ.data.allowed.length > 0 && (
                      <div className="rounded-lg border bg-white">
                        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Allowed ({whoQ.data.allowed.length})</div>
                        {whoQ.data.allowed.map((p) => <WhoRow key={p.principalId} p={p} />)}
                      </div>
                    )}

                    {/* Deliberately its own box below the allowed list, never merged into it. */}
                    {whoQ.data.indeterminate.length > 0 && (
                      <div className="rounded-lg border border-amber-300 bg-white">
                        <div className="border-b border-amber-200 bg-amber-50 px-3 py-2 text-sm font-semibold text-amber-900">
                          Cannot be determined ({whoQ.data.indeterminate.length})
                          <span className="ml-2 text-[11px] font-normal text-amber-800">
                            an unevaluated condition or an uncollected role sits in the path — these are not a no
                          </span>
                        </div>
                        {whoQ.data.indeterminate.map((p) => <WhoRow key={p.principalId} p={p} />)}
                      </div>
                    )}
                  </>
                )}
              </>
            )}

            {mode === "reach" && (
              <>
                {!principalId && <div className="rounded-lg border bg-white p-4 text-sm text-gray-600">Pick a principal.</div>}
                {reachQ.isLoading && <div className="text-sm text-gray-500">Collecting grants…</div>}
                {reachQ.isError && <div className="text-sm text-red-600">Could not read this principal's access.</div>}
                {reachQ.data && (
                  <div className="space-y-3 rounded-lg border bg-white p-3">
                    <div className="text-sm text-gray-800">
                      <b className="tabular-nums">{reachQ.data.control.length}</b> control-plane and{" "}
                      <b className="tabular-nums">{reachQ.data.data.length}</b> data-plane role assignment(s) at or above{" "}
                      <span className="font-mono text-[11px]">{reachQ.data.scope}</span>.
                    </div>
                    {reachQ.data.control.length === 0 && reachQ.data.data.length === 0 && (
                      <div className="rounded border bg-gray-50 px-2 py-1 text-[11px] text-gray-600">
                        No grants found here. Roles held further down the tree are not shown — this answers
                        &ldquo;at or above this scope&rdquo;.
                      </div>
                    )}
                    <RefList title="Control plane" refs={reachQ.data.control} />
                    <RefList title="Data plane" refs={reachQ.data.data} />
                    <RefList title="Deny assignments" refs={reachQ.data.denies} tone="text-red-700" />
                    {reachQ.data.unknownRoles.length > 0 && (
                      <div className="rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-900">
                        Role definition(s) not collected, so their permissions are unknown:{" "}
                        <b>{reachQ.data.unknownRoles.join(", ")}</b>.
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
