import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type EntraApp360, type EntraAppRow } from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import { AppRegistrationsView } from "../AppRegistrationsView";
import {
  Bar, EntraEmpty, SevBadge, useSubTabRoute,
  SEVERITY_RANK, SortScopeNote, SortTh, cmp, useEntraSorted, useSortState, type SortState,
} from "./EntraShared";

/**
 * Application 360.
 *
 * The distinction this screen exists to make: `requested` permissions are what an app asks
 * for, `granted` is what it actually has. Only the granted set is risk, and conflating the
 * two produces a wall of false positives that trains people to ignore the screen.
 */

// "registrations" is the former /identity/app-registrations screen. It answers a different
// question from `inventory`: inventory is about the permissions an app *holds* (risk), while
// registrations is about the credentials an app *expires on* (operational hygiene). Both are
// about applications, so they belong on the same screen rather than in a separate product.
const TABS = ["inventory", "consent", "registrations"] as const;
const TAB_LABELS: Record<(typeof TABS)[number], string> = {
  inventory: "Inventory",
  consent: "Consent",
  registrations: "Application Registrations",
};

const TIER_CHIP: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  high: "bg-orange-100 text-orange-700",
  medium: "bg-amber-100 text-amber-700",
  low: "bg-gray-100 text-gray-500",
};

// ---------------------------------------------------------------------------- sorting
// The inventory grid pages server-side, so its columns sort server-side too — the keys
// below are exactly the ones `GET /entra/apps` accepts. Sorting the loaded page in the
// browser would silently mean "top by credentials among the 200 rows risk picked".
type AppsSortKey = "risk" | "name" | "permissions" | "credentials" | "owners" | "assigned" | "tier";
/** What the server already does when asked for nothing. Never sent as a parameter. */
const APPS_SORT_DEFAULT: SortState<AppsSortKey> = { key: "risk", dir: -1 };

type ConsentGrant = { client: string; resource: string; scopes: string[]; max_tier: string };
type ConsentSortKey = "client" | "resource" | "scopes" | "tier";
// The server returns these tier-descending then client-ascending; starting there keeps the
// first render identical to the unsorted screen.
const CONSENT_SORT_DEFAULT: SortState<ConsentSortKey> = { key: "tier", dir: -1 };
/** Stable identity for the empty case, so the memo does not re-sort on every render. */
const NO_GRANTS: ConsentGrant[] = [];

function compareConsent(a: ConsentGrant, b: ConsentGrant, key: ConsentSortKey): number {
  switch (key) {
    case "client": return cmp.text(a.client, b.client);
    case "resource": return cmp.text(a.resource, b.resource);
    // A grant's weight is how many scopes it carries, not how they happen to spell.
    case "scopes": return cmp.num(a.scopes?.length ?? null, b.scopes?.length ?? null);
    // Permission tiers use the critical/high/medium/low vocabulary, so the shared
    // severity ranks apply verbatim — alphabetical would file `high` below `low`.
    case "tier": return cmp.rank(SEVERITY_RANK, a.max_tier, b.max_tier);
    default: return 0;
  }
}

type AppCredential = EntraApp360["credentials"][number];
// "none" is the untouched state: Graph returns credentials in no particular order and this
// panel must open showing exactly that, so the default comparator is a no-op.
type CredSortKey = "none" | "name" | "kind" | "expiry";
const CRED_SORT_DEFAULT: SortState<CredSortKey> = { key: "none", dir: -1 };

function compareCredential(a: AppCredential, b: AppCredential, key: CredSortKey): number {
  switch (key) {
    case "name": return cmp.text(a.display_name || a.id || a.kind, b.display_name || b.id || b.kind);
    case "kind": return cmp.text(a.kind, b.kind);
    // The end date, not `days_left`: a credential with no recorded expiry is unknown,
    // and cmp.date sinks it rather than pretending it expires today.
    case "expiry": return cmp.date(a.end, b.end);
    default: return 0;
  }
}

export function EntraAppsView({ connectionId }: { connectionId: string | null }) {
  const [tab, setTab] = useSubTabRoute(TABS, "inventory");
  return (
    // h-full, not flex-1: the parent is EntraView's plain scroll box, not a flex column, so
    // flex-1 resolves to nothing there and this root would grow to its full content height.
    // h-full resolves against that box's definite height and bounds the tabs below it.
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b bg-white px-4">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-2 text-[13px] font-medium capitalize ${
              tab === t ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>
      {/* The native tabs are plain documents that scroll as one block. The re-parented panel
          pins its own header and scrolls only its body, so it needs a *bounded* box — given a
          scrolling one it grows to full content height and its header scrolls away. So the
          container clips and each tab opts into the scrolling it actually wants. */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === "registrations" ? (
          <div className="flex h-full min-h-0 flex-col">
            <AppRegistrationsView connectionId={connectionId} />
          </div>
        ) : (
          <div className="h-full overflow-auto">
            {tab === "inventory" && <Inventory connectionId={connectionId} />}
            {tab === "consent" && <Consent connectionId={connectionId} />}
          </div>
        )}
      </div>
    </div>
  );
}

function Inventory({ connectionId }: { connectionId: string | null }) {
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 150);
  const [ownerless, setOwnerless] = useState(false);
  const [riskMin, setRiskMin] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [sort, setSort] = useSortState<AppsSortKey>("apps-inventory", APPS_SORT_DEFAULT);
  // This grid is a server-side page. Asking for the default explicitly would work, but
  // sending nothing is what actually guarantees the server's own risk-first ordering.
  const serverSorted = sort.key !== APPS_SORT_DEFAULT.key || sort.dir !== APPS_SORT_DEFAULT.dir;

  const q = useQuery({
    queryKey: ["entra-apps", connectionId, dSearch, ownerless, riskMin, sort.key, sort.dir],
    queryFn: () => {
      // Built as a variable, not passed inline: api.ts still declares the pre-sort param
      // shape, and TypeScript only excess-property-checks fresh object literals.
      const params = {
        search: dSearch || undefined,
        ownerless: ownerless || undefined,
        risk_min: riskMin,
        sort: serverSorted ? sort.key : undefined,
        dir: serverSorted ? (sort.dir === 1 ? "asc" : "desc") : undefined,
      };
      return api.entraApps(params, connectionId);
    },
  });

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading applications…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  return (
    <div className="p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by name or app id…"
          className="w-72 rounded border px-2 py-1 text-sm"
        />
        <label className="flex items-center gap-1 text-xs text-gray-600">
          <input type="checkbox" checked={ownerless} onChange={(e) => setOwnerless(e.target.checked)} />
          Ownerless only
        </label>
        <label className="flex items-center gap-1 text-xs text-gray-600">
          Risk ≥
          <input
            type="number" min={0} max={100} value={riskMin}
            onChange={(e) => setRiskMin(Number(e.target.value) || 0)}
            className="w-16 rounded border px-1 py-0.5 text-xs"
          />
        </label>
        <span className="ml-auto text-xs text-gray-400">
          {d.total.toLocaleString()} application(s){serverSorted ? "" : " · sorted by risk"}
        </span>
      </div>

      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
              <SortTh label="Risk" col="risk" sort={sort} setSort={setSort} className="px-3" />
              <SortTh label="Application" col="name" sort={sort} setSort={setSort} className="px-2" firstDir={1} />
              <SortTh label="Permissions" col="permissions" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by the number of granted permissions" />
              <SortTh label="Credentials" col="credentials" sort={sort} setSort={setSort} className="px-2" />
              <SortTh label="Owners" col="owners" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by owner count — applications whose owners could not be read are not counted as zero" />
              <SortTh label="Assigned" col="assigned" sort={sort} setSort={setSort} className="px-2" />
            </tr>
          </thead>
          <tbody>
            {d.apps.map((a) => (
              <AppRow key={a.object_id + a.app_id} app={a} onOpen={() => setSelected(a.object_id)} />
            ))}
          </tbody>
        </table>
        {/* Only fires when the server capped the page. */}
        <SortScopeNote shown={d.apps.length} total={d.total} />
      </div>

      {selected && (
        <App360Drawer objectId={selected} connectionId={connectionId} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function AppRow({ app, onOpen }: { app: EntraAppRow; onOpen: () => void }) {
  const tone = app.risk_score >= 80 ? "bg-red-500" : app.risk_score >= 50 ? "bg-amber-500" : "bg-gray-400";
  return (
    <tr onClick={onOpen} className="cursor-pointer border-b last:border-b-0 hover:bg-gray-50">
      <td className="w-28 px-3 py-1.5">
        <div className="flex items-center gap-1.5">
          <Bar value={app.risk_score} tone={tone} />
          <span className="w-6 text-right text-xs font-medium text-gray-700">{app.risk_score}</span>
        </div>
      </td>
      <td className="px-2 py-1.5">
        <div className="text-gray-900">{app.display_name || app.app_id}</div>
        <div className="flex flex-wrap gap-1 text-[11px] text-gray-400">
          {app.platform_managed ? (
            <span title="Azure owns this identity's credentials and ownership, so it is not scored on either.">
              Azure-managed identity
            </span>
          ) : (
            !app.has_registration && <span>enterprise app only</span>
          )}
          {app.orphaned && <span className="text-amber-600">orphaned</span>}
          {app.multi_tenant && <span>multi-tenant</span>}
          {app.is_external && <span>external publisher</span>}
        </div>
      </td>
      <td className="px-2 py-1.5">
        {app.granted_permissions ? (
          <span className={`rounded px-1.5 py-0.5 text-[11px] ${TIER_CHIP[app.max_permission_tier] ?? ""}`}>
            {app.granted_permissions} granted · {app.max_permission_tier}
          </span>
        ) : (
          <span className="text-xs text-gray-400">none</span>
        )}
        {app.consent_grant_capable && (
          <span className="ml-1 rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">self-grant</span>
        )}
        {app.tenant_wide && (
          <span className="ml-1 rounded bg-orange-100 px-1.5 py-0.5 text-[11px] text-orange-700">tenant-wide</span>
        )}
      </td>
      <td className="px-2 py-1.5 text-gray-600">
        {app.credential_count}
        {app.expired_credentials > 0 && (
          <span className="ml-1 text-red-600">({app.expired_credentials} expired)</span>
        )}
        {app.expiring_credentials > 0 && (
          <span className="ml-1 text-amber-600">({app.expiring_credentials} expiring)</span>
        )}
      </td>
      <td className="px-2 py-1.5">
        {app.owners_known ? (
          app.owner_count ? (
            <span className="text-gray-600">{app.owner_count}</span>
          ) : (
            <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700">none</span>
          )
        ) : (
          <span className="text-xs text-gray-400">unknown</span>
        )}
      </td>
      <td className="px-2 py-1.5 text-gray-600">{app.assigned_principals || "—"}</td>
    </tr>
  );
}

function App360Drawer({
  objectId,
  connectionId,
  onClose,
}: {
  objectId: string;
  connectionId: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["entra-app360", objectId, connectionId],
    queryFn: () => api.entraApp360(objectId, connectionId),
  });
  return (
    <div className="fixed inset-y-0 right-0 z-30 w-[34rem] overflow-auto border-l bg-white shadow-xl">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="text-sm font-semibold">Application 360</div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-700">✕</button>
      </div>
      <div className="p-4 text-[13px]">
        {q.isLoading && <div className="text-gray-500">Loading…</div>}
        {q.isError && <div className="text-red-600">{formatError(q.error)}</div>}
        {q.data && <App360Body data={q.data} />}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-4">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-400">{title}</div>
      {children}
    </div>
  );
}

function App360Body({ data }: { data: EntraApp360 }) {
  const a = data.app;
  return (
    <>
      <div className="text-base font-semibold text-gray-900">{a.display_name}</div>
      <div className="text-xs text-gray-500">
        appId {a.app_id} · {a.sign_in_audience || a.sp_type}
        {a.verified_publisher ? ` · publisher ${a.verified_publisher}` : " · unverified publisher"}
      </div>

      {/* Risk, with every component published so the number is never a black box. */}
      <Section title={`Risk ${data.risk?.score ?? 0}/100`}>
        <div className="space-y-1">
          {(data.risk?.components ?? []).map((c) => (
            <div key={c.key} className="flex items-center gap-2">
              <span className="w-44 shrink-0 truncate text-xs text-gray-500" title={c.label}>{c.label}</span>
              <Bar value={c.points} max={c.weight} tone={c.points > 0 ? "bg-red-400" : "bg-gray-300"} />
              <span className="w-12 text-right text-xs text-gray-600">
                {c.not_applicable ? "n/a" : `${c.points}/${c.weight}`}
              </span>
            </div>
          ))}
        </div>
        {(data.risk?.components ?? []).some((c) => c.not_applicable) && (
          <div className="mt-2 text-xs text-gray-500">
            {(data.risk?.components ?? []).find((c) => c.not_applicable)?.not_applicable}
          </div>
        )}
      </Section>

      <Section title="Ownership">
        {data.owners.length ? (
          <ul className="list-disc pl-5 text-gray-700">
            {data.owners.map((o) => <li key={o.id}>{o.name}</li>)}
          </ul>
        ) : (
          <div className="rounded bg-red-50 px-2 py-1 text-red-700">
            No owner — nobody is accountable for rotating credentials or retiring this application.
          </div>
        )}
      </Section>

      <Section title={`Granted application permissions (${data.granted_application_permissions.length})`}>
        {data.granted_application_permissions.length ? (
          <div className="flex flex-wrap gap-1">
            {data.granted_application_permissions.map((p, i) => (
              <span key={i} className={`rounded px-1.5 py-0.5 text-[11px] ${TIER_CHIP[p.tier] ?? ""}`}
                    title={`${p.resource} · ${p.tier}`}>
                {p.permission}
              </span>
            ))}
          </div>
        ) : (
          <div className="text-gray-500">None granted.</div>
        )}
      </Section>

      {data.granted_delegated.length > 0 && (
        <Section title="Delegated consent">
          <ul className="space-y-1">
            {data.granted_delegated.map((g, i) => (
              <li key={i} className="text-gray-700">
                <span className={g.consent_type === "AllPrincipals" ? "font-medium text-orange-700" : ""}>
                  {g.consent_type === "AllPrincipals" ? "tenant-wide" : "per-user"}
                </span>{" "}
                — {g.scopes.join(", ")}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.requested_not_granted.length > 0 && (
        <Section title="Requested but NOT granted">
          <div className="flex flex-wrap gap-1">
            {data.requested_not_granted.map((p, i) => (
              <span key={i} className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">
                {p.permission}
              </span>
            ))}
          </div>
          <div className="mt-1 text-[11px] text-gray-400">
            These are asked for in the manifest but carry no grant — they are not risk today.
          </div>
        </Section>
      )}

      <Section title={`Credentials (${data.credentials.length})`}>
        {data.credentials.length ? (
          <CredentialsTable credentials={data.credentials} />
        ) : (
          <div className="text-gray-500">No credentials.</div>
        )}
      </Section>

      {data.federated_credentials.length > 0 && (
        <Section title="Federated identity credentials">
          <ul className="space-y-1">
            {data.federated_credentials.map((f, i) => (
              <li key={i} className={f.trusted && !f.wildcard_subject ? "text-gray-700" : "text-red-700"}>
                {f.name} — {f.issuer}
                {f.wildcard_subject && " (wildcard subject)"}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Conditional Access">
        {data.conditional_access.enforced_policies === 0 ? (
          <div className="text-gray-500">No enforced policies exist in this tenant.</div>
        ) : data.conditional_access.covered_by.length ? (
          <div className="text-gray-700">{data.conditional_access.covered_by.join(", ")}</div>
        ) : (
          <div className="rounded bg-amber-50 px-2 py-1 text-amber-800">
            Not covered by any enforced policy.
          </div>
        )}
      </Section>

      {(data.azure_reach.role_count > 0 || data.azure_reach.roles.length > 0) && (
        <Section title="Azure reach">
          <div className="text-gray-700">
            {data.azure_reach.roles.join(", ") || `${data.azure_reach.role_count} role(s)`}
          </div>
          <div className="text-[11px] text-gray-400">
            From the Azure RBAC cache{data.azure_reach.stale ? " (older than this snapshot)" : ""}.
          </div>
        </Section>
      )}

      {data.provisioning.length > 0 && (
        <Section title="Provisioning">
          {data.provisioning.map((j, i) => (
            <div key={i} className={j.quarantine ? "text-red-700" : "text-gray-700"}>
              {j.template || j.id}: {j.code}
              {j.quarantine && " (quarantined)"}
            </div>
          ))}
        </Section>
      )}

      {data.findings.length > 0 && (
        <Section title={`Findings (${data.findings.length})`}>
          <ul className="space-y-1">
            {data.findings.map((f) => (
              <li key={f.fingerprint} className="flex items-start gap-2">
                <SevBadge sev={f.severity} />
                <span className="text-gray-700">{f.title}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </>
  );
}

/**
 * The credential list, sortable by expiry.
 *
 * Its own component because the drawer renders it conditionally and a hook cannot live
 * behind an `&&`. Expiry is the column that matters: this panel exists to answer "what
 * breaks next", which the collection order of Graph never happens to answer.
 */
function CredentialsTable({ credentials }: { credentials: AppCredential[] }) {
  const [sort, setSort] = useSortState<CredSortKey>("app360-credentials", CRED_SORT_DEFAULT);
  const rows = useEntraSorted(credentials, sort, compareCredential);
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="border-b text-left text-[11px] text-gray-500">
          <SortTh label="Credential" col="name" sort={sort} setSort={setSort} firstDir={1} />
          <SortTh label="Kind" col="kind" sort={sort} setSort={setSort} firstDir={1} />
          <SortTh label="Expiry" col="expiry" sort={sort} setSort={setSort} align="right"
                  title="Sort by expiry date — credentials with no recorded expiry sort last" />
        </tr>
      </thead>
      <tbody>
        {rows.map((c, i) => (
          <tr key={i} className="border-b last:border-b-0">
            <td className="py-1 text-gray-700">{c.display_name || c.id || c.kind}</td>
            <td className="py-1 text-gray-500">{c.kind}</td>
            <td className="py-1 text-right">
              {c.expired ? (
                <span className="text-red-600">expired</span>
              ) : c.days_left == null ? (
                <span className="text-gray-500">—</span>
              ) : c.days_left <= 90 ? (
                <span className="text-amber-600">{c.days_left}d left</span>
              ) : c.days_left > 365 * 3 ? (
                // "34785d" is not a number anyone can read, and a multi-decade secret
                // is a finding in itself, not a neutral fact.
                <span
                  className="text-amber-600"
                  title={`This credential is valid for about ${Math.round(c.days_left / 365)} years. Long-lived secrets cannot be rotated on any sensible schedule.`}
                >
                  ~{Math.round(c.days_left / 365)}y lifetime
                </span>
              ) : (
                <span className="text-gray-500">{c.days_left.toLocaleString()}d</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Consent({ connectionId }: { connectionId: string | null }) {
  const [sort, setSort] = useSortState<ConsentSortKey>("apps-consent-grants", CONSENT_SORT_DEFAULT);
  const q = useQuery({
    queryKey: ["entra-apps-consent", connectionId],
    queryFn: () => api.entraAppsConsent(connectionId),
  });
  // Sorted before the early returns, because a hook cannot sit behind a loading branch.
  const grants = useEntraSorted(q.data?.all_principals_grants ?? NO_GRANTS, sort, compareConsent);
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;
  const policy = (d.authorization_policy ?? {}) as {
    user_consent_unrestricted?: boolean;
    user_consent_disabled?: boolean;
    allow_invites_from?: string;
  };
  const adminConsent = (d.admin_consent_policy ?? {}) as { is_enabled?: boolean };

  return (
    <div className="space-y-4 p-4">
      <div className="rounded-lg border bg-white p-3">
        <div className="mb-2 text-[13px] font-semibold text-gray-800">Tenant consent posture</div>
        <Row label="User consent">
          {policy.user_consent_unrestricted ? (
            <span className="font-medium text-red-700">Users may consent to any application</span>
          ) : policy.user_consent_disabled ? (
            <span className="text-green-700">Disabled — administrators consent only</span>
          ) : (
            <span className="text-gray-700">Restricted to low-risk permissions</span>
          )}
        </Row>
        <Row label="Admin consent workflow">
          {adminConsent.is_enabled ? (
            <span className="text-green-700">Enabled</span>
          ) : (
            <span className="text-amber-700">Disabled — users have no route to request an app</span>
          )}
        </Row>
        <Row label="Guest invitations">{policy.allow_invites_from || "unknown"}</Row>
      </div>

      <div className="rounded-lg border bg-white">
        <div className="border-b px-4 py-2 text-[13px] font-semibold text-gray-800">
          Tenant-wide delegated grants ({d.all_principals_grants.length})
        </div>
        <div className="px-4 py-2 text-xs text-gray-500">
          A grant consented for all principals behaves like an application permission — it applies to
          everyone who signs in, not just the person who accepted it.
        </div>
        {d.all_principals_grants.length ? (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b bg-gray-50 text-left text-xs text-gray-500">
                <SortTh label="Application" col="client" sort={sort} setSort={setSort} className="px-3" firstDir={1} />
                <SortTh label="Resource" col="resource" sort={sort} setSort={setSort} className="px-2" firstDir={1} />
                <SortTh label="Scopes" col="scopes" sort={sort} setSort={setSort} className="px-2"
                        title="Sort by how many scopes the grant carries" />
                <SortTh label="Tier" col="tier" sort={sort} setSort={setSort} className="px-2" />
              </tr>
            </thead>
            <tbody>
              {grants.map((g, i) => (
                <tr key={i} className="border-b last:border-b-0">
                  <td className="px-3 py-1.5 text-gray-900">{g.client}</td>
                  <td className="px-2 py-1.5 text-gray-600">{g.resource}</td>
                  <td className="px-2 py-1.5 text-gray-700">{g.scopes.join(", ")}</td>
                  <td className="px-2 py-1.5">
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${TIER_CHIP[g.max_tier] ?? ""}`}>
                      {g.max_tier}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="px-4 py-6 text-center text-sm text-gray-400">No tenant-wide delegated grants.</div>
        )}
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 py-0.5 text-[13px]">
      <span className="w-48 shrink-0 text-gray-500">{label}</span>
      <span>{children}</span>
    </div>
  );
}
