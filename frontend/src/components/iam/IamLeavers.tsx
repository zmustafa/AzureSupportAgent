/** Disabled Access tab — *"who is disabled in Entra ID but still holds access somewhere?"*
 *
 * The only PERSON-centric screen in this feature. Every other lens is one row per grant, which
 * is the right shape for "what is granted" and the wrong one for "who should not still be here":
 * a leaver holding Contributor on four subscriptions is one offboarding task, not four findings.
 *
 * Rules, each of which is a defect if broken:
 *
 *  - `measured === false` renders a WALL, never an empty table. "No disabled account holds
 *    access" is the most reassuring sentence this feature can produce and it must never be
 *    produced by never having asked.
 *  - the denominator is always on screen. "78 disabled identities" means nothing without "out
 *    of 1,227 principals holding access, 25 of which we could not check".
 *  - group header counts come from the SERVER's count maps, never from the loaded page. A
 *    page-derived count shrinks as the reader scrolls.
 *  - the tiers are labelled by what is TRUE of them, not by alarm level. Most of this access is
 *    dormant — a disabled account cannot obtain a token — and overstating it to make the screen
 *    feel urgent is how a security tool gets ignored.
 *  - "not measured" is never rendered as "never". Sign-in and usage both come from separate
 *    scans with their own cadence, and a blank there is an absence of evidence.
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  api,
  type IamLeaverGrant,
  type IamLeaverIdentity,
  type IamLeaverResource,
  type IamLeaversFilter,
  type IamLeaversRemediation,
  type IamLeaversReport,
} from "../../api";
import { usePersistedState } from "../../utils/persistedState";
import { useGroupedCollapse, type GroupDimension } from "../../utils/useGroupedCollapse";
import { useIamConnectionId } from "./IamShared";
import {
  ageLabel,
  dayOf,
  grantScopeContext,
  grantScopeLabel,
  identityAsMarkdown,
  resourceGroupKey,
  resourceLabel,
  shortRole,
} from "./leaversDisplay";

const TIER_STYLE: Record<string, string> = {
  live_now: "border-red-300 bg-red-50",
  restorable: "border-amber-300 bg-amber-50",
};

/** Height of a collapsed row or a group header. Both truncate rather than wrap, so this is a
 *  fact about the layout rather than a guess — see the `min-w-0 truncate` on the role cell. */
const COLLAPSED_ROW_H = 41;

const ON_PREM_OPTIONS = [
  { value: "", label: "Any directory" },
  { value: "cloud", label: "Cloud-only" },
  { value: "onprem", label: "Synced from on-prem AD" },
  // Its own option on purpose: an unknown sync state sends an operator to the wrong console,
  // so it must be findable rather than silently filed under "cloud".
  { value: "unknown", label: "Sync state unknown" },
];

const SIGNIN_KIND_LABELS: Record<string, string> = {
  any: "Any sign-in",
  interactive: "Interactive only",
  nonInteractive: "Non-interactive only",
  successful: "Successful only",
  servicePrincipal: "Service principal",
};

// Which API a remediation step lands on. Named on screen because they are NOT interchangeable:
// `az role assignment delete` removes an Azure RBAC assignment held directly by the principal,
// and does nothing at all against a group-derived grant or an Entra directory role.
const PLANE_LABELS: Record<string, string> = {
  azure_rbac: "Azure RBAC",
  group_membership: "Group membership",
  entra_directory_role: "Entra directory role",
  service_principal_owner: "App ownership",
  pim_eligible: "PIM eligibility",
  key_vault_access_policy: "Key Vault policy",
  classic_admin: "Classic admin (manual)",
  lighthouse: "Lighthouse (manual)",
  deny_assignment: "Deny assignment (manual)",
};

function Chip({ children, tone = "gray" }: { children: React.ReactNode; tone?: string }) {
  const cls =
    tone === "red"
      ? "bg-red-100 text-red-800"
      : tone === "amber"
        ? "bg-amber-100 text-amber-800"
        : tone === "sky"
          ? "bg-sky-100 text-sky-800"
          : "bg-gray-100 text-gray-700";
  return <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>{children}</span>;
}

/** One copyable script block.
 *
 * Its own Copy button, because the revoke and the undo are separate documents run at separate
 * times. The button confirms it copied — silent success on a clipboard action leaves the
 * operator re-clicking, and re-clicking a script is how people end up pasting twice. */
function ScriptBlock({
  title,
  text,
  tone,
  testId,
  note,
}: {
  title: string;
  text: string;
  tone: "rose" | "emerald";
  testId: string;
  note?: string;
}) {
  const [copied, setCopied] = useState(false);
  const accent = tone === "rose" ? "text-rose-800" : "text-emerald-800";
  const lines = text.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).length;

  return (
    <div data-testid={testId}>
      <div className="flex items-center gap-2 text-[11px]">
        <span className={`font-semibold ${accent}`}>{title}</span>
        <span className="text-gray-500">
          {lines} command{lines === 1 ? "" : "s"}
        </span>
        {note && <span className="text-gray-500">— {note}</span>}
        <button
          type="button"
          className="ml-auto rounded border px-1.5 py-0.5 text-gray-600 hover:bg-gray-50"
          data-testid={`${testId}-copy`}
          onClick={async () => {
            await navigator.clipboard?.writeText(text);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="mt-1 max-h-64 overflow-auto rounded bg-gray-50 p-2 font-mono text-[10px] text-gray-700">
        {text}
      </pre>
    </div>
  );
}

/** The Where panel: Subscription ▸ resource type ▸ resource, every level expandable.
 *
 * Replaces a flat list of display strings truncated to six with a "+N more" that was not a
 * control. The reader could not group by resource type, could not see the rest, and could not
 * recover the ARM id of the thing they were being asked to act on. */
function ResourcePanel({ resources }: { resources: IamLeaverResource[] }) {
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());
  const [showIds, setShowIds] = useState(false);

  const bySub = useMemo(() => {
    const subs = new Map<string, Map<string, IamLeaverResource[]>>();
    for (const r of resources) {
      const sub = r.subscriptionName || r.subscriptionId || "Directory / tenant-wide";
      const kind = resourceGroupKey(r);
      const inner = subs.get(sub) ?? new Map<string, IamLeaverResource[]>();
      inner.set(kind, [...(inner.get(kind) ?? []), r]);
      subs.set(sub, inner);
    }
    return subs;
  }, [resources]);

  if (resources.length === 0) return <div className="text-gray-500">—</div>;

  return (
    <div className="space-y-1" data-testid="leaver-resources">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-gray-700">
          Where ({resources.length} scope{resources.length === 1 ? "" : "s"})
        </span>
        <label className="flex items-center gap-1 text-[10px] text-gray-500">
          <input type="checkbox" checked={showIds} onChange={(e) => setShowIds(e.target.checked)} />
          Show resource ids
        </label>
      </div>
      {[...bySub.entries()].map(([sub, kinds]) => (
        <div key={sub} className="rounded border bg-white">
          <div className="border-b bg-gray-50 px-2 py-1 text-[11px] font-medium text-gray-700">
            {sub}
          </div>
          {[...kinds.entries()].map(([kind, list]) => {
            const key = `${sub}::${kind}`;
            const open = openKeys.has(key);
            return (
              <div key={key} className="border-b last:border-b-0">
                <button
                  type="button"
                  className="flex w-full items-center gap-1 px-2 py-1 text-left text-[11px] hover:bg-gray-50"
                  onClick={() =>
                    setOpenKeys((prev) => {
                      const next = new Set(prev);
                      if (next.has(key)) next.delete(key);
                      else next.add(key);
                      return next;
                    })
                  }
                >
                  <span className="text-gray-400">{open ? "▾" : "▸"}</span>
                  <span className="font-medium text-gray-700">{kind}</span>
                  <span className="text-gray-500">({list.length})</span>
                  {list.some((r) => r.privileged > 0) && <Chip tone="red">privileged</Chip>}
                </button>
                {open && (
                  <ul className="space-y-0.5 px-2 pb-1 pl-6">
                    {list.map((r) => (
                      <li key={r.scope} className="text-[11px]">
                        <span className="text-gray-800">{resourceLabel(r)}</span>
                        {/* The resource group is CONTEXT for a resource, and it IS the label
                            for a resource-group scope. Printing it in both places rendered
                            every resource-group row as "<name> in <name>". */}
                        {r.resourceGroup && r.scopeType === "resource" && (
                          <span className="ml-1 text-gray-400">in {r.resourceGroup}</span>
                        )}
                        <span className="ml-1 text-gray-500">
                          — {r.roles.map(shortRole).join(", ")}
                        </span>
                        <span className="ml-1 text-gray-400">
                          {r.direct ? "(direct)" : `(via ${r.viaGroups.join(", ")})`}
                        </span>
                        {showIds && (
                          <div className="flex items-start gap-1">
                            <code className="break-all font-mono text-[10px] text-gray-400">
                              {r.scope}
                            </code>
                            <button
                              type="button"
                              className="shrink-0 text-[10px] text-brand hover:underline"
                              onClick={() => navigator.clipboard?.writeText(r.scope)}
                            >
                              copy
                            </button>
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** The actual grants. Aggregates alone force the reader back to the Access grid to answer
 *  "which assignment, exactly?" — the question every one of these rows ends in. */
function GrantTable({ grants, truncated }: { grants: IamLeaverGrant[]; truncated: boolean }) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? grants : grants.slice(0, 8);
  return (
    <div data-testid="leaver-grants">
      <div className="mb-0.5 font-semibold text-gray-700">Grants ({grants.length})</div>
      <table className="w-full table-fixed text-[10px]">
        <thead>
          <tr className="text-left text-gray-500">
            <th className="w-[30%] font-medium">Role</th>
            <th className="w-[34%] font-medium">Scope</th>
            <th className="w-[20%] font-medium">Held via</th>
            <th className="w-[16%] font-medium">Granted</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((g) => (
            <tr key={`${g.assignmentId}|${g.scope}|${g.roleDefinitionId}`} className="align-top">
              <td className="truncate pr-1 text-gray-800" title={g.roleName}>
                {g.roleIsPrivileged && <span className="mr-0.5 text-red-600">⚠</span>}
                {shortRole(g.roleName)}
              </td>
              <td className="truncate pr-1 text-gray-600" title={g.scope}>
                {grantScopeLabel(g)}
                {grantScopeContext(g) && (
                  <span className="ml-1 text-gray-400">{grantScopeContext(g)}</span>
                )}
              </td>
              <td className="truncate pr-1 text-gray-500">
                {g.accessPath === "GroupTransitive" ? g.sourceGroupName || "group" : "direct"}
              </td>
              <td className="text-gray-500">{dayOf(g.assignmentCreatedOn)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {grants.length > 8 && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="mt-0.5 text-[10px] text-brand hover:underline"
        >
          {showAll ? "Show fewer" : `Show all ${grants.length}`}
        </button>
      )}
      {truncated && (
        <div className="mt-0.5 text-[10px] text-amber-800">
          This list is capped. The counts above cover every grant; this table does not.
        </div>
      )}
    </div>
  );
}

function IdentityRow({
  i,
  tierLabel,
  dormancyLabel,
  selected,
  onSelect,
  open,
  onToggle,
  onMeasured,
}: {
  i: IamLeaverIdentity;
  tierLabel: string;
  dormancyLabel: string;
  selected: boolean;
  onSelect: (id: string, on: boolean) => void;
  open: boolean;
  onToggle: () => void;
  onMeasured: (height: number) => void;
}) {
  const [copied, setCopied] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  // Reports the height the list has to reserve. It measures the PARENT — the virtual item
  // wrapper — not this card, because that is the element the virtualizer positions and it
  // carries the row gap. Measuring the card instead left the gap unreserved and the next row
  // sat 4px too high, which is the same bug in miniature. Runs before paint so the rows below
  // land in the right place on the first frame rather than jumping.
  useLayoutEffect(() => {
    const el = rootRef.current?.parentElement ?? rootRef.current;
    if (open && el) onMeasured(el.offsetHeight);
  });
  return (
    <div ref={rootRef} className="rounded border bg-white" data-testid="leaver-identity">
      <div className="flex w-full items-center gap-2 px-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={(e) => onSelect(i.principalId, e.target.checked)}
          aria-label={`Select ${i.displayName || i.principalId}`}
          data-testid="leaver-select"
          className="shrink-0"
        />
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 py-2 text-left hover:bg-gray-50"
        >
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800" title={i.principalId}>
            {i.displayName || i.principalId}
            {/* Real tenants contain DISTINCT principals with IDENTICAL display names, and this
                one has two objects with the same name where only one carries a UPN. On a screen
                whose entire purpose is "go and remove this person's access", two visually
                identical rows is a wrong-account risk. */}
            {i.userPrincipalName ? (
              <span className="ml-1 text-gray-400">{i.userPrincipalName}</span>
            ) : (
              <span className="ml-1 font-mono text-[10px] text-gray-400">
                {i.principalId.slice(0, 8)}…
              </span>
            )}
          </span>
          <Chip tone={i.tier === "live_now" ? "red" : "amber"}>{tierLabel}</Chip>
          {/* These ride on the COLLAPSED row on purpose: group-derived access is the least
              visible case in the product, and on-prem sync changes WHERE the fix goes. */}
          {i.groupGrants > 0 && i.directGrants === 0 && <Chip tone="sky">via group</Chip>}
          {i.softDeleted && <Chip tone="red">recycle bin</Chip>}
          {i.onPremSynced === "true" && <Chip>on-prem</Chip>}
          {/* Only ever shown when a graph HAS been built. Rendering its absence would say
              "cannot escalate" about a tenant nobody has analysed. */}
          {i.escalationMeasured && i.escalationPaths > 0 && (
            <Chip tone="red">{i.escalationPaths} escalation</Chip>
          )}
          <span className="shrink-0 text-[11px] text-gray-600">
            {i.grants} grant{i.grants === 1 ? "" : "s"}
          </span>
          {i.privilegedGrants > 0 && <Chip tone="red">{i.privilegedGrants} privileged</Chip>}
          {/* min-w-0 + truncate, NOT shrink-0: a Key Vault access-policy role name is several
              hundred characters on a real tenant, and without truncation it wrapped the row. */}
          <span
            className="min-w-0 max-w-[22ch] shrink truncate text-[11px] text-gray-500"
            title={i.highestRole}
          >
            {shortRole(i.highestRole)}
          </span>
          <span className="w-12 shrink-0 text-right text-[10px] text-gray-400" title={dormancyLabel}>
            {i.dormancyDays !== null ? ageLabel(i.dormancyDays) : ""}
          </span>
        </button>
      </div>
      {open && (
        <div className="space-y-2 border-t px-3 py-2 text-[11px]">
          <div className="flex flex-wrap items-center gap-1">
            <Chip>{i.principalType}</Chip>
            {i.userType && <Chip>{i.userType}</Chip>}
            {i.onPremSynced === "true" && <Chip tone="sky">Synced from on-prem AD</Chip>}
            {i.onPremSynced === "unknown" && <Chip>Sync state unknown</Chip>}
            {i.pimEligible > 0 && (
              <Chip tone="amber">
                {i.pimEligible} PIM eligible
                {i.permanentlyEligible > 0 ? ` (${i.permanentlyEligible} permanent)` : ""}
              </Chip>
            )}
            <button
              type="button"
              className="ml-auto rounded border px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50"
              onClick={() => {
                navigator.clipboard?.writeText(identityAsMarkdown(i, tierLabel, dormancyLabel));
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? "Copied" : "Copy as ticket"}
            </button>
          </div>

          {i.onPremSynced === "true" && (
            <div className="rounded bg-sky-50 p-2 text-sky-900">
              This account is mastered in on-premises Active Directory. Remove the access here,
              but any account-state change must be made in AD or the next sync reverts it.
            </div>
          )}
          {i.softDeleted && (
            <div className="rounded bg-red-50 p-2 text-red-900">
              <div className="font-semibold">In the Entra ID recycle bin</div>
              Deleted{i.deletedDateTime ? ` on ${dayOf(i.deletedDateTime)}` : ""}, and recoverable
              for 30 days. Restoring the object restores every grant below at once, so these are
              not harmless orphans — remove the assignments rather than waiting for the retention
              window to expire.
            </div>
          )}
          {i.ownedDetail.length > 0 && (
            <div className="rounded bg-red-50 p-2 text-red-900">
              <div className="font-semibold">Live now, not dormant</div>
              A service principal signs in with its own secret or certificate, so disabling this
              user's account did not stop it. Reassign ownership and roll the credential —
              removing the owner alone leaves the existing secret valid.
              <ul className="mt-1 space-y-0.5">
                {i.ownedDetail.map((o) => (
                  <li key={o.principalId}>
                    <span className="font-medium">{o.name}</span>
                    <span className="ml-1 text-red-800">
                      {o.lastSignIn
                        ? `— last signed in ${dayOf(o.lastSignIn)}`
                        : o.lastSignInKnown
                          ? "— not seen inside the sign-in report's window (not the same as never)"
                          : "— app sign-in activity not measured"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <div>
                <div className="font-semibold text-gray-700">How the access is held</div>
                <div className="text-gray-600">
                  {i.directGrants} directly · {i.groupGrants} through a group
                </div>
                {i.groupsGrantingAccess.length > 0 && (
                  <div className="mt-1 text-gray-600">
                    Groups: {i.groupsGrantingAccess.join(", ")}
                    <div className="mt-0.5 text-amber-800">
                      Remove the member from the group — do not delete the group's assignment, it
                      serves everyone else in it.
                    </div>
                  </div>
                )}
              </div>
              <div data-testid="leaver-evidence">
                <div className="font-semibold text-gray-700" data-testid="leaver-evidence-title">
                  Evidence of life
                </div>
                <table className="text-[10px]">
                  <tbody>
                    {[
                      ["Interactive", i.signIn.interactive],
                      ["Non-interactive", i.signIn.nonInteractive],
                      ["Successful", i.signIn.successful],
                      ["Owned app", i.signIn.servicePrincipal],
                    ].map(([label, value]) => (
                      <tr key={label as string}>
                        <td className="pr-2 text-gray-500">{label}</td>
                        <td className="text-gray-700">
                          {value
                            ? dayOf(value as string)
                            : i.signIn.known
                              ? "none recorded"
                              : "not measured"}
                        </td>
                      </tr>
                    ))}
                    <tr>
                      <td className="pr-2 text-gray-500">Last used</td>
                      <td className="text-gray-700">
                        {!i.activityMeasured
                          ? "not measured"
                          : i.lastActivity
                            ? `${dayOf(i.lastActivity)} (${i.activityEvents} operation${
                                i.activityEvents === 1 ? "" : "s"
                              })`
                            : i.activityConclusive
                              ? "no operations recorded in the window"
                              : // The distinction that stops a deletion being justified by a
                                // window that could never have seen the activity.
                                "cannot be concluded from this window"}
                      </td>
                    </tr>
                    <tr>
                      <td className="pr-2 text-gray-500">Access granted</td>
                      <td className="text-gray-700">
                        {dayOf(i.oldestGrantAt)}
                        {i.newestGrantAt && i.newestGrantAt !== i.oldestGrantAt
                          ? ` – ${dayOf(i.newestGrantAt)}`
                          : ""}
                      </td>
                    </tr>
                  </tbody>
                </table>
                {i.lastSignInSource && (
                  <div className="mt-0.5 text-[10px] text-gray-400">
                    Sign-in from the {i.lastSignInSource}.
                  </div>
                )}
              </div>
            </div>
            <ResourcePanel resources={i.resources} />
          </div>

          <GrantTable grants={i.grantDetail} truncated={i.grantDetailTruncated} />
          <div className="font-mono text-[10px] text-gray-400">{i.principalId}</div>
        </div>
      )}
    </div>
  );
}

/** A saved filter set. Deliberately local: these encode how one operator works, not a tenant
 *  fact, and syncing them would need a server model nobody has asked for. */
type SavedView = { name: string; filter: IamLeaversFilter };

type Line =
  | { kind: "header"; key: string; label: string; total: number; fromPage: boolean; depth: number }
  | { kind: "row"; key: string; item: IamLeaverIdentity };

export function LeaversTab() {
  const connectionId = useIamConnectionId();
  const navigate = useNavigate();

  const [tier, setTier] = useState("");
  const [principalType, setPrincipalType] = useState("");
  const [privilegedOnly, setPrivilegedOnly] = useState(false);
  const [onPrem, setOnPrem] = useState("");
  const [viaGroupOnly, setViaGroupOnly] = useState(false);
  const [softDeleted, setSoftDeleted] = useState(false);
  const [hasOwnedSp, setHasOwnedSp] = useState(false);
  const [pimEligible, setPimEligible] = useState(false);
  const [neverUsed, setNeverUsed] = useState(false);
  const [dormancy, setDormancy] = useState("");
  const [signinKind, setSigninKind] = useState("any");
  const [subscription, setSubscription] = useState("");
  const [role, setRole] = useState("");
  const [plane, setPlane] = useState("");
  const [group, setGroup] = useState("");
  const [search, setSearch] = useState("");
  const [showAdvanced, setShowAdvanced] = usePersistedState<boolean>("iam.leavers.advanced", false);
  const [views, setViews] = usePersistedState<SavedView[]>("iam.leavers.views", []);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const [preview, setPreview] = useState<IamLeaversRemediation | null>(null);
  const [previewing, setPreviewing] = useState(false);

  // ONE filter object drives the query AND both download hrefs, so the file can never contain
  // rows the screen was not showing. The main access export shipped exactly that regression
  // once, when the privileged lens moved into client state.
  const filter: IamLeaversFilter = useMemo(
    () => ({
      tier: tier || undefined,
      principal_type: principalType || undefined,
      privileged_only: privilegedOnly || undefined,
      on_prem: onPrem || undefined,
      via_group_only: viaGroupOnly || undefined,
      soft_deleted: softDeleted || undefined,
      has_owned_sp: hasOwnedSp || undefined,
      pim_eligible: pimEligible || undefined,
      never_used: neverUsed || undefined,
      dormancy: dormancy || undefined,
      signin_kind: signinKind,
      subscription: subscription || undefined,
      role: role || undefined,
      plane: plane || undefined,
      group: group || undefined,
      search: search.trim() || undefined,
      connection_id: connectionId,
    }),
    [tier, principalType, privilegedOnly, onPrem, viaGroupOnly, softDeleted, hasOwnedSp,
      pimEligible, neverUsed, dormancy, signinKind, subscription, role, plane, group, search,
      connectionId],
  );

  const applyView = useCallback((f: IamLeaversFilter) => {
    setTier(f.tier ?? "");
    setPrincipalType(f.principal_type ?? "");
    setPrivilegedOnly(!!f.privileged_only);
    setOnPrem(f.on_prem ?? "");
    setViaGroupOnly(!!f.via_group_only);
    setSoftDeleted(!!f.soft_deleted);
    setHasOwnedSp(!!f.has_owned_sp);
    setPimEligible(!!f.pim_eligible);
    setNeverUsed(!!f.never_used);
    setDormancy(f.dormancy ?? "");
    setSigninKind(f.signin_kind ?? "any");
    setSubscription(f.subscription ?? "");
    setRole(f.role ?? "");
    setPlane(f.plane ?? "");
    setGroup(f.group ?? "");
    setSearch(f.search ?? "");
  }, []);

  const q = useQuery({
    queryKey: ["iam", "leavers", filter],
    queryFn: () => api.iamLeavers(filter),
    // Don't flash an empty screen while a filter change is in flight.
    placeholderData: (prev) => prev,
  });
  const d: IamLeaversReport | undefined = q.data;
  const identities = useMemo(() => d?.identities ?? [], [d]);
  const counts = useMemo(() => d?.counts ?? {}, [d]);
  const dormancyLabels = useMemo(() => d?.dormancy_labels ?? {}, [d]);

  const dimensions = useMemo<GroupDimension<IamLeaverIdentity>[]>(
    () => [
      { key: "none", label: "No grouping", of: () => "all" },
      {
        key: "tier",
        label: "Exposure",
        of: (i) => i.tier,
        labelOf: (k) => d?.tiers?.[k]?.label ?? k,
        counts: counts.tier,
      },
      {
        key: "on_prem",
        label: "Directory",
        of: (i) => i.onPremSynced,
        labelOf: (k) =>
          k === "true"
            ? "Synced from on-prem AD"
            : k === "false"
              ? "Cloud-only"
              : "Sync state unknown",
        counts: counts.on_prem,
      },
      {
        key: "dormancy",
        label: "Dormancy",
        of: (i) => i.dormancyBucket,
        labelOf: (k) => dormancyLabels[k] ?? k,
        counts: counts.dormancy,
      },
      {
        key: "principal_type",
        label: "Principal type",
        of: (i) => i.principalType,
        counts: counts.principal_type,
      },
      {
        key: "highest_role",
        label: "Highest role",
        of: (i) => i.highestRole,
        labelOf: shortRole,
        counts: counts.highest_role,
      },
      { key: "subscription", label: "Subscription", of: (i) => i.subscriptions, counts: counts.subscription },
      { key: "plane", label: "Plane", of: (i) => i.planes, counts: counts.plane },
      { key: "group", label: "Granting group", of: (i) => i.groupsGrantingAccess, counts: counts.group },
    ],
    [counts, d, dormancyLabels],
  );

  const grouping = useGroupedCollapse(identities, dimensions, {
    storagePrefix: "iam.leavers",
    defaultGroupBy: "none",
  });

  // Flatten to a single render list so ONE virtualizer covers headers and rows alike. Without
  // it, 78 expandable rows is fine and a 5,000-leaver tenant is not.
  const lines = useMemo<Line[]>(() => {
    if (grouping.groupBy === "none" || !grouping.sections) {
      return identities.map((i) => ({ kind: "row" as const, key: i.principalId, item: i }));
    }
    const out: Line[] = [];
    for (const s of grouping.sections) {
      out.push({
        kind: "header",
        key: s.key,
        label: s.label,
        total: s.total,
        fromPage: s.countIsFromPage,
        depth: 0,
      });
      if (grouping.isCollapsed(s.key)) continue;
      if (s.subGroups) {
        for (const sub of s.subGroups) {
          out.push({
            kind: "header",
            key: sub.key,
            label: sub.label,
            total: sub.items.length,
            fromPage: true,
            depth: 1,
          });
          if (grouping.isCollapsed(sub.key)) continue;
          for (const i of sub.items) {
            out.push({ kind: "row", key: `${sub.key}|${i.principalId}`, item: i });
          }
        }
      } else {
        for (const i of s.items) out.push({ kind: "row", key: `${s.key}|${i.principalId}`, item: i });
      }
    }
    return out;
  }, [grouping, identities]);

  const scrollRef = useRef<HTMLDivElement>(null);
  // Which row is expanded, and how tall it actually turned out to be.
  //
  // The virtualizer positions every LATER row from the size it has recorded for this one, so an
  // expanded row that is still recorded as 41px is drawn straight over the rows beneath it. Its
  // own ResizeObserver did not report the growth here — `measureElement` on the grown node was a
  // no-op — so the size is supplied from state instead of discovered. `measure()` is the
  // documented way to invalidate, and one source of truth beats two that can disagree.
  const [openRow, setOpenRow] = useState<{ key: string; height: number } | null>(null);
  const virt = useVirtualizer({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    // Collapsed rows and group headers are a fixed height by construction: every cell in them
    // truncates rather than wraps, which is enforced by the `min-w-0 truncate` on the role cell.
    estimateSize: (index) =>
      openRow && lines[index]?.key === openRow.key ? openRow.height : COLLAPSED_ROW_H,
    overscan: 10,
  });

  useLayoutEffect(() => {
    virt.measure();
  }, [virt, openRow]);

  const toggleRow = useCallback((key: string) => {
    // Opening starts from an estimate; the row reports its real height on the next commit and
    // the list settles. Guessing high would leave a gap, guessing low would overlap — so the
    // estimate only has to survive a single frame.
    setOpenRow((cur) => (cur?.key === key ? null : { key, height: COLLAPSED_ROW_H * 8 }));
  }, []);

  const setRowHeight = useCallback((key: string, height: number) => {
    setOpenRow((cur) => (cur && cur.key === key && cur.height !== height ? { key, height } : cur));
  }, []);

  const onSelect = useCallback((id: string, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  // Add or remove exactly the identities the current filter is showing. Ids selected under a
  // previous filter are left alone rather than dropped — narrowing a filter should not silently
  // destroy a selection you might come back to.
  const onSelectAll = useCallback(
    (on: boolean) => {
      setSelected((prev) => {
        const next = new Set(prev);
        for (const i of identities) {
          if (on) next.add(i.principalId);
          else next.delete(i.principalId);
        }
        return next;
      });
    },
    [identities],
  );

  // The selection INTERSECTED with what the filter is showing. Keeping the raw set would let a
  // count of "5 selected" sit above a list of 3, and would send principal_ids the filter
  // excludes — the server ANDs the two, so the export would come back empty while the screen
  // still claimed five. What is counted here is what every action will actually cover.
  const effectiveSelection = useMemo(() => {
    if (!selected.size) return [] as string[];
    const inView = new Set(identities.map((i) => i.principalId));
    return [...selected].filter((id) => inView.has(id));
  }, [selected, identities]);

  // The filter set PLUS any explicit row selection. Everything that acts on "what is on
  // screen" — the campaign, the remediation preview, the exports — uses this one object, so
  // none of them can quietly cover a different population from the list being looked at.
  const selectorFilter: IamLeaversFilter = useMemo(
    () =>
      effectiveSelection.length ? { ...filter, principal_ids: effectiveSelection } : filter,
    [filter, effectiveSelection],
  );

  async function startReview() {
    setStarting(true);
    setStartError("");
    try {
      // The WHOLE filter set, plus any explicit selection. Sending only `tier` and
      // `privileged_only` — which is what this did — produced a campaign covering 78
      // identities from a screen showing 3. The selector is evaluated server-side and
      // re-evaluated when the campaign is refreshed, so it stays a live control rather than a
      // frozen list; the population it was created with is recorded on the campaign so a
      // reviewer can see whether it has grown.
      const { connection_id: _conn, ...scope } = selectorFilter;
      const created = await api.iamCreateCampaign(
        {
          name: `Offboarding review — disabled accounts with access`,
          description:
            "Access still held by accounts disabled in Entra ID. Azure does not revoke role " +
            "assignments when an account is disabled, so re-enabling the account restores all " +
            "of it with no approval.",
          selector: { kind: "disabled", ...scope },
        },
        connectionId,
      );
      navigate(`/iam/reviews?campaign=${encodeURIComponent(created.campaign.id)}`);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  async function showPreview() {
    setPreviewing(true);
    setStartError("");
    try {
      setPreview(await api.iamLeaversRemediation("az", selectorFilter));
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewing(false);
    }
  }

  if (q.isLoading && !d) return <div className="p-4 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-4 text-sm text-red-700">{String(q.error)}</div>;
  if (!d) return <div className="p-4 text-sm text-gray-500">No data.</div>;

  // A wall, not an empty table. See the module docstring.
  if (!d.measured) {
    return (
      <div className="p-4" data-testid="leavers-not-measured">
        <div className="rounded border border-amber-300 bg-amber-50 p-4">
          <div className="text-sm font-semibold text-amber-900">Not measured</div>
          <p className="mt-1 max-w-2xl text-xs text-amber-900">{d.reason}</p>
          <p className="mt-2 max-w-2xl text-xs text-amber-800">
            This is not the same as "no disabled account holds access".{" "}
            {d.denominator.principals_with_access} principal(s) hold access in this tenant and
            none of them could be checked against the directory.
          </p>
        </div>
      </div>
    );
  }

  const t = d.totals;
  const shown = identities.length;
  const selCount = effectiveSelection.length;
  const allSelected = shown > 0 && selCount === shown;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b p-3">
        <div className="text-sm text-gray-800" data-testid="leavers-headline">
          <span className="font-semibold">{t.identities ?? 0}</span> disabled identit
          {t.identities === 1 ? "y" : "ies"} still hold{" "}
          <span className="font-semibold">{t.grants ?? 0}</span> grant(s),{" "}
          <span className="font-semibold">{t.privileged_grants ?? 0}</span> of them privileged,
          across {t.subscriptions_touched ?? 0} subscription(s).
        </div>
        <div className="text-[11px] text-gray-500">
          Out of {d.denominator.principals_with_access} principal(s) holding access:{" "}
          {d.denominator.state_resolved} checked, {d.denominator.state_unknown} could not be
          checked, {d.denominator.not_applicable} have no account state (groups).
          {d.denominator.state_unknown > 0 && (
            <span className="ml-1 text-amber-800">
              The {d.denominator.state_unknown} unchecked are absent from this report, not cleared
              by it.
            </span>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {Object.entries(d.tiers).map(([key, meta]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTier(tier === key ? "" : key)}
              className={`rounded border p-2 text-left ${TIER_STYLE[key] ?? "border-gray-300 bg-gray-50"} ${
                tier === key ? "ring-2 ring-brand" : ""
              }`}
              style={{ maxWidth: 380 }}
            >
              <div className="text-xs font-semibold text-gray-800">
                {meta.label} · {d.tier_counts[key] ?? 0}
              </div>
              <div className="mt-0.5 text-[10px] leading-snug text-gray-600">{meta.detail}</div>
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name, UPN, role, group, object id…"
            aria-label="Search disabled identities"
            className="w-56 rounded border px-2 py-1"
          />
          <select
            value={principalType}
            onChange={(e) => setPrincipalType(e.target.value)}
            aria-label="Principal type"
            className="rounded border px-2 py-1"
          >
            <option value="">All types</option>
            <option value="User">User</option>
            <option value="ServicePrincipal">Service principal</option>
          </select>
          <select
            value={onPrem}
            onChange={(e) => setOnPrem(e.target.value)}
            aria-label="Directory"
            className="rounded border px-2 py-1"
          >
            {ON_PREM_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={privilegedOnly}
              onChange={(e) => setPrivilegedOnly(e.target.checked)}
            />
            Privileged only
          </label>
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={viaGroupOnly}
              onChange={(e) => setViaGroupOnly(e.target.checked)}
            />
            Group-only access
          </label>
          <button
            type="button"
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
            data-testid="leavers-more-filters"
          >
            {showAdvanced ? "Fewer filters" : "More filters"}
          </button>
          <span className="ml-auto flex items-center gap-1">
            <button
              type="button"
              onClick={startReview}
              disabled={starting || shown === 0}
              className="rounded border border-brand px-2 py-1 font-medium text-brand hover:bg-brand/5 disabled:opacity-50"
              data-testid="leavers-start-review"
              title="Create a certification campaign over exactly these identities, on the Reviews tab"
            >
              {starting ? "Starting…" : "Start a review"}
            </button>
            <button
              type="button"
              onClick={showPreview}
              disabled={previewing || shown === 0}
              className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              data-testid="leavers-preview-remediation"
              title="Show the ordered revocation script for these identities. Nothing is run."
            >
              {previewing ? "Building…" : "Preview script"}
            </button>
            <span className="text-gray-500">Export</span>
            <a
              href={api.iamLeaversExportUrl("csv", "identities", selectorFilter)}
              className="rounded border px-2 py-1 hover:bg-gray-50"
              data-testid="leavers-export-identities-csv"
            >
              People (CSV)
            </a>
            <a
              href={api.iamLeaversExportUrl("csv", "grants", selectorFilter)}
              className="rounded border px-2 py-1 hover:bg-gray-50"
              data-testid="leavers-export-grants-csv"
            >
              Grants (CSV)
            </a>
            <a
              href={api.iamLeaversExportUrl("xlsx", "identities", selectorFilter)}
              className="rounded border px-2 py-1 hover:bg-gray-50"
              data-testid="leavers-export-xlsx"
            >
              Workbook (XLSX)
            </a>
          </span>
        </div>

        {showAdvanced && (
          <div
            className="flex flex-wrap items-center gap-2 rounded bg-gray-50 p-2 text-xs"
            data-testid="leavers-advanced-filters"
          >
            <select
              value={dormancy}
              onChange={(e) => setDormancy(e.target.value)}
              aria-label="Dormancy"
              className="rounded border px-2 py-1"
            >
              <option value="">Any dormancy</option>
              {Object.entries(dormancyLabels).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                  {counts.dormancy?.[k] !== undefined ? ` (${counts.dormancy[k]})` : ""}
                </option>
              ))}
            </select>
            <select
              value={signinKind}
              onChange={(e) => setSigninKind(e.target.value)}
              aria-label="Sign-in kind"
              className="rounded border px-2 py-1"
              disabled={!dormancy}
              title="Which sign-in timestamp the dormancy filter is measured from"
            >
              {(d.facets.signin_kinds ?? []).map((k) => (
                <option key={k} value={k}>
                  {SIGNIN_KIND_LABELS[k] ?? k}
                </option>
              ))}
            </select>
            <select
              value={subscription}
              onChange={(e) => setSubscription(e.target.value)}
              aria-label="Subscription"
              className="max-w-[16rem] rounded border px-2 py-1"
            >
              <option value="">All subscriptions</option>
              {d.facets.subscriptions.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              aria-label="Role"
              className="max-w-[16rem] rounded border px-2 py-1"
            >
              <option value="">All roles</option>
              {d.facets.roles.map((r) => (
                <option key={r} value={r}>
                  {shortRole(r)}
                </option>
              ))}
            </select>
            <select
              value={plane}
              onChange={(e) => setPlane(e.target.value)}
              aria-label="Plane"
              className="rounded border px-2 py-1"
            >
              <option value="">All planes</option>
              {d.facets.planes.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <select
              value={group}
              onChange={(e) => setGroup(e.target.value)}
              aria-label="Granting group"
              className="max-w-[16rem] rounded border px-2 py-1"
            >
              <option value="">Any group</option>
              {d.facets.groups.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={softDeleted}
                onChange={(e) => setSoftDeleted(e.target.checked)}
              />
              In recycle bin
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={hasOwnedSp}
                onChange={(e) => setHasOwnedSp(e.target.checked)}
              />
              Owns a service principal
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={pimEligible}
                onChange={(e) => setPimEligible(e.target.checked)}
              />
              PIM eligible
            </label>
            {/* Disabled when nothing can be concluded. A "never used" filter over a truncated
                sweep, or over a window that closes before the account was last alive, returns
                people whose access nobody has evidence about — and this is the filter most
                likely to end in a deletion. */}
            <label
              className={`flex items-center gap-1 ${
                d.usage.available && !d.usage.truncated ? "" : "opacity-50"
              }`}
              title={
                !d.usage.available
                  ? "The Activity Log usage sweep has not run for this tenant"
                  : d.usage.truncated
                    ? "The last sweep hit the Activity Log's 6 MB per-subscription cap, so an absent operation is not evidence of disuse. Re-run it over a shorter window."
                    : `No operation recorded in a window that covers the account's own lifetime (last ${d.usage.window_days} days)`
              }
            >
              <input
                type="checkbox"
                checked={neverUsed}
                disabled={!d.usage.available || d.usage.truncated}
                onChange={(e) => setNeverUsed(e.target.checked)}
              />
              Never used the access
              {d.usage.available && !d.usage.truncated && (
                <span className="text-gray-400">({t.never_used ?? 0})</span>
              )}
            </label>

            <span className="ml-auto flex items-center gap-1">
              <select
                aria-label="Saved views"
                value=""
                onChange={(e) => {
                  const v = views.find((x) => x.name === e.target.value);
                  if (v) applyView(v.filter);
                }}
                className="rounded border px-2 py-1"
              >
                <option value="">Saved views…</option>
                {views.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
                onClick={() => {
                  const name = window.prompt("Name this view");
                  if (!name) return;
                  const rest = { ...filter };
                  delete rest.connection_id;
                  setViews([...views.filter((v) => v.name !== name), { name, filter: rest }]);
                }}
              >
                Save view
              </button>
            </span>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <label className="flex items-center gap-1">
            <span className="text-gray-500">Group by</span>
            <select
              value={grouping.groupBy}
              onChange={(e) => grouping.setGroupBy(e.target.value)}
              aria-label="Group identities"
              className="rounded border px-2 py-1"
            >
              {dimensions.map((dim) => (
                <option key={dim.key} value={dim.key}>
                  {dim.label}
                </option>
              ))}
            </select>
          </label>
          {grouping.groupBy !== "none" && (
            <>
              <label className="flex items-center gap-1">
                <span className="text-gray-500">then</span>
                <select
                  value={grouping.subGroupBy}
                  onChange={(e) => grouping.setSubGroupBy(e.target.value)}
                  aria-label="Sub-group identities"
                  className="rounded border px-2 py-1"
                >
                  {dimensions
                    .filter((dim) => dim.key !== grouping.groupBy)
                    .map((dim) => (
                      <option key={dim.key} value={dim.key}>
                        {dim.label}
                      </option>
                    ))}
                </select>
              </label>
              <button
                type="button"
                onClick={grouping.collapseAll}
                className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
              >
                Collapse all
              </button>
              <button
                type="button"
                onClick={grouping.expandAll}
                className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
              >
                Expand all
              </button>
            </>
          )}
          {shown > 0 && (
            <label
              className="flex items-center gap-1"
              title={
                allSelected
                  ? "Clear the selection"
                  : `Select the ${shown} identit${shown === 1 ? "y" : "ies"} this filter is showing`
              }
            >
              <input
                type="checkbox"
                checked={allSelected}
                // Indeterminate is not an attribute — it only exists on the DOM node, and
                // without it a partial selection is indistinguishable from none.
                ref={(el) => {
                  if (el) el.indeterminate = selCount > 0 && !allSelected;
                }}
                onChange={(e) => onSelectAll(e.target.checked)}
                aria-label={`Select all ${shown} shown`}
                data-testid="leavers-select-all"
              />
              <span className="text-gray-600">Select all {shown}</span>
            </label>
          )}
          {selCount > 0 && (
            <span className="flex items-center gap-1">
              <span className="text-gray-600">{selCount} selected</span>
              <button
                type="button"
                className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
                onClick={() => {
                  const picked = identities.filter((i) => selected.has(i.principalId));
                  navigator.clipboard?.writeText(
                    picked
                      .map((i) =>
                        identityAsMarkdown(
                          i,
                          d.tiers[i.tier]?.label ?? i.tier,
                          dormancyLabels[i.dormancyBucket] ?? "",
                        ),
                      )
                      .join("\n\n---\n\n"),
                  );
                }}
              >
                Copy as ticket
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
              >
                Clear
              </button>
            </span>
          )}
          <span className="ml-auto text-[11px] text-gray-500">
            Showing {shown} of {d.total_identities} disabled identit
            {d.total_identities === 1 ? "y" : "ies"}.
          </span>
        </div>
        {startError && (
          <div className="rounded bg-red-50 px-2 py-1 text-[11px] text-red-800">{startError}</div>
        )}
        {selCount > 0 && (
          <div
            className="rounded bg-sky-50 px-2 py-1 text-[11px] text-sky-900"
            data-testid="leavers-selection-banner"
          >
            {selCount} identit{selCount === 1 ? "y" : "ies"} selected — the exports, the review
            and the script now cover only these.
          </div>
        )}
        {preview && (
          <div className="rounded border bg-white p-2" data-testid="leavers-remediation-preview">
            <div className="flex items-center gap-2 text-[11px]">
              <span className="font-semibold text-gray-700">Revocation script</span>
              {preview.measured ? (
                <span className="text-gray-600">
                  {preview.action_count} step(s) over {preview.identities} identit
                  {preview.identities === 1 ? "y" : "ies"}
                  {preview.grants > preview.action_count && (
                    <> — {preview.grants} grants, folded where one change covers several</>
                  )}
                </span>
              ) : (
                <span className="text-amber-800">{preview.reason}</span>
              )}
              <button
                type="button"
                className="ml-auto rounded border px-1.5 py-0.5 text-gray-600 hover:bg-gray-50"
                onClick={() => setPreview(null)}
              >
                Close
              </button>
            </div>
            {/* Said on the screen as well as in the script header: this product does not run
                anything against Azure, and an operator who assumes otherwise is the worst
                possible misunderstanding for a tool that generates deletions. */}
            <div className="mt-1 text-[10px] text-amber-800">
              Nothing here is run by the product. Group-derived access is revoked before direct
              assignments, and every step carries a dry run and a rollback.
            </div>
            {/* Which API each step lands on. `az role assignment delete` only ever removes an
                Azure RBAC assignment held DIRECTLY by the principal — pointed at a group-derived
                grant or a directory role it exits cleanly having done nothing, and the operator
                ticks the line off. Naming the planes up front is how that stays visible. */}
            {preview.measured && preview.planes && (
              <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
                {Object.entries(preview.planes).map(([p, n]) => (
                  <span key={p} className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">
                    {PLANE_LABELS[p] ?? p}: {n}
                  </span>
                ))}
                {Object.keys(preview.planes).length > 1 && (
                  <span className="text-gray-500">
                    these are different APIs — the steps are not interchangeable
                  </span>
                )}
              </div>
            )}
            {/* Two blocks, not one. The revoke and the undo are run at different times by
                different people, and a single scrollable blob means whoever reaches for the
                rollback has to select the right half of it by hand — under pressure. */}
            {preview.script && (
              <div className="mt-1 space-y-2">
                <ScriptBlock
                  title="1. Remove the access"
                  tone="rose"
                  text={preview.revoke_script ?? preview.script}
                  testId="leavers-script-revoke"
                />
                {preview.rollback_script && (
                  <ScriptBlock
                    title="2. Undo (rollback)"
                    tone="emerald"
                    text={preview.rollback_script}
                    testId="leavers-script-rollback"
                    note="Keep this with the change record before you run step 1."
                  />
                )}
              </div>
            )}
          </div>
        )}
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto p-3">
        {shown === 0 ? (
          <div className="rounded border bg-white p-4 text-xs text-gray-600">
            {d.total_identities === 0
              ? "No disabled principal holds access in this tenant. Account state was collected, so this is a measured result."
              : "No disabled identity matches these filters."}
          </div>
        ) : (
          <div style={{ height: virt.getTotalSize(), position: "relative" }}>
            {virt.getVirtualItems().map((v) => {
              const line = lines[v.index];
              return (
                <div
                  key={line.key}
                  ref={virt.measureElement}
                  data-index={v.index}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${v.start}px)`,
                  }}
                  className="pb-1"
                >
                  {line.kind === "header" ? (
                    <button
                      type="button"
                      onClick={() => grouping.toggle(line.key)}
                      style={{ paddingLeft: 8 + line.depth * 16 }}
                      className="flex w-full items-center gap-2 rounded bg-gray-100 py-1 pr-2 text-left text-xs font-medium text-gray-700 hover:bg-gray-200"
                      data-testid="leaver-group-header"
                    >
                      <span className="text-gray-400">
                        {grouping.isCollapsed(line.key) ? "▸" : "▾"}
                      </span>
                      <span className="truncate">{line.label || "—"}</span>
                      {/* A count that came from the page rather than the server says so, instead
                          of quietly disagreeing with the section under it. */}
                      <span className="text-gray-500">
                        {line.total}
                        {line.fromPage && line.depth === 0 ? " shown" : ""}
                      </span>
                    </button>
                  ) : (
                    <IdentityRow
                      i={line.item}
                      tierLabel={d.tiers[line.item.tier]?.label ?? line.item.tier}
                      dormancyLabel={dormancyLabels[line.item.dormancyBucket] ?? ""}
                      selected={selected.has(line.item.principalId)}
                      onSelect={onSelect}
                      open={openRow?.key === line.key}
                      onToggle={() => toggleRow(line.key)}
                      onMeasured={(h) => setRowHeight(line.key, h)}
                    />
                  )}
                </div>
              );
            })}
          </div>
        )}

        {d.limitations.length > 0 && (
          <details className="mt-3 rounded border bg-gray-50 p-2 text-[11px] text-gray-600">
            <summary className="cursor-pointer font-medium text-gray-700">
              What this report cannot tell you ({d.limitations.length})
            </summary>
            <ul className="mt-1 list-disc space-y-1 pl-4">
              {d.limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
