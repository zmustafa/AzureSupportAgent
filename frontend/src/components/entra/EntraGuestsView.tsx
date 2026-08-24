/** Guest (B2B) hygiene — the whole external population, its lifecycle and its partner orgs.
 *
 * Three renderings here are structural rather than cosmetic, because each one is a place the
 * screen could otherwise state something confidently false:
 *
 *  - **"Not measured" is its own column, never folded into dormant.** A guest whose sign-in
 *    activity was not collected has not been shown to be unused; grading it would send
 *    somebody to revoke live access.
 *  - **Human and token activity are separate columns.** `lastNonInteractiveSignInDateTime`
 *    moves on refresh, so a guest who left the partner months ago passes any "last sign-in"
 *    report. On a real estate this routinely accounts for a LARGE share of the apparently
 *    active guest population.
 *  - **Partner governance shows `unknown` when the cross-tenant list could not be read.**
 *    Rendering every partner as "ungoverned" because we could not look would be the loudest
 *    false claim on the page. A domain that simply has no Entra tenant behind it is a
 *    separate, honestly-labeled case.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type { EntraGuestDomain, EntraGuestRow, EntraGuests } from "../../api";
import { formatError } from "../../utils/format";
import { cmp, CoverageBanner, EntraEmpty, SortTh, useEntraSorted, useSortState } from "./EntraShared";
import { InvestigateLink } from "./InvestigateLink";

const LIFECYCLE_META: Record<string, { label: string; cls: string; why: string }> = {
  pending: {
    label: "Invitation pending", cls: "bg-amber-100 text-amber-900",
    why: "Invited, never accepted. A directory object nobody needs.",
  },
  accepted_never_used: {
    label: "Accepted, never used", cls: "bg-orange-100 text-orange-900",
    why: "The identity is live and carries what it was granted, but nobody has ever used it.",
  },
  dormant: {
    label: "Dormant", cls: "bg-rose-100 text-rose-900",
    why: "Used once, not since the dormancy window.",
  },
  active: {
    label: "Active", cls: "bg-emerald-100 text-emerald-900",
    why: "Signed in inside the dormancy window.",
  },
  unknown: {
    label: "Not measured", cls: "bg-gray-100 text-gray-600",
    why: "Sign-in activity was not collected. This is NOT evidence the account is unused.",
  },
};

const CLASS_META: Record<string, { label: string; cls: string }> = {
  corporate: { label: "Corporate", cls: "text-gray-600" },
  consumer: { label: "Consumer email", cls: "text-rose-700" },
  government: { label: "Government", cls: "text-sky-700" },
  education: { label: "Education", cls: "text-violet-700" },
  unresolved: { label: "Unresolved", cls: "text-gray-400" },
};

const GOV_META: Record<string, { label: string; cls: string }> = {
  governed: { label: "Named in policy", cls: "text-emerald-700" },
  default_only: { label: "Default only", cls: "text-amber-700" },
  unknown: { label: "Unknown", cls: "text-gray-400" },
};

function days(n: number | null | undefined): string {
  return n == null ? "—" : n === 0 ? "today" : `${n}d`;
}

function Tile({ label, value, tone, title }: {
  label: string; value: number; tone?: string; title?: string;
}) {
  return (
    <div className="rounded-lg border bg-white px-2.5 py-1.5" title={title}>
      <div className={`text-lg font-semibold leading-tight ${tone || "text-gray-900"}`}>
        {value.toLocaleString()}
      </div>
      <div className="truncate text-[10px] uppercase leading-tight tracking-wide text-gray-500">{label}</div>
    </div>
  );
}

/** Invited -> accepted -> used -> still active, with the leak named at each step. */
function Funnel({ c }: { c: EntraGuests["counts"] }) {
  const steps = [
    { label: "Invited", value: c.invited, lost: c.pending, lostLabel: "never accepted" },
    { label: "Accepted", value: c.accepted, lost: c.never_used, lostLabel: "never used" },
    { label: "Used it", value: c.accepted - c.never_used, lost: c.dormant, lostLabel: "now dormant" },
    { label: "Still active", value: c.active, lost: 0, lostLabel: "" },
  ];
  const max = Math.max(1, c.invited);
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 text-xs font-semibold text-gray-800">Guest lifecycle</div>
      <div className="space-y-1.5">
        {steps.map((s) => (
          <div key={s.label} className="flex items-center gap-2">
            <div className="w-24 shrink-0 text-[11px] text-gray-600">{s.label}</div>
            <div className="h-4 min-w-0 flex-1 rounded bg-gray-100">
              <div className="h-4 rounded bg-brand/70" style={{ width: `${(s.value / max) * 100}%` }} />
            </div>
            <div className="w-16 shrink-0 text-right text-[11px] tabular-nums text-gray-800">
              {s.value.toLocaleString()}
            </div>
            <div className="w-32 shrink-0 text-[11px] text-rose-700">
              {s.lost > 0 ? `−${s.lost.toLocaleString()} ${s.lostLabel}` : ""}
            </div>
          </div>
        ))}
      </div>
      {c.not_measured > 0 && (
        <div className="mt-2 text-[11px] text-gray-500">
          {c.not_measured.toLocaleString()} guest(s) are excluded from the funnel because their
          sign-in activity was not collected — that is an absence of measurement, not a lifecycle
          outcome.
        </div>
      )}
    </div>
  );
}

type GuestKey = "" | "name" | "domain" | "lifecycle" | "invited" | "human" | "any" | "enabled";
type DomainKey = "" | "domain" | "guests" | "pending" | "dormant" | "oldest" | "governance";

function guestCmp(a: EntraGuestRow, b: EntraGuestRow, key: GuestKey): number {
  switch (key) {
    case "name": return cmp.text(a.display_name || a.upn, b.display_name || b.upn);
    case "domain": return cmp.text(a.domain, b.domain);
    case "lifecycle": return cmp.text(a.lifecycle, b.lifecycle);
    case "invited": return cmp.num(a.invited_days_ago, b.invited_days_ago);
    case "human": return cmp.num(a.last_human_days_ago, b.last_human_days_ago);
    case "any": return cmp.num(a.last_any_days_ago, b.last_any_days_ago);
    case "enabled": return cmp.num(Number(a.enabled), Number(b.enabled));
    default: return 0;
  }
}

function domainCmp(a: EntraGuestDomain, b: EntraGuestDomain, key: DomainKey): number {
  switch (key) {
    case "domain": return cmp.text(a.partner_name || a.domain, b.partner_name || b.domain);
    case "guests": return cmp.num(a.guests, b.guests);
    case "pending": return cmp.num(a.pending, b.pending);
    case "dormant": return cmp.num(a.dormant, b.dormant);
    case "oldest": return cmp.num(a.oldest_invite_days, b.oldest_invite_days);
    case "governance": return cmp.text(a.governance || "", b.governance || "");
    default: return 0;
  }
}

export function EntraGuestsView({ connectionId }: { connectionId?: string | null }) {
  const q = useQuery({
    queryKey: ["entra", "guests", connectionId ?? ""],
    queryFn: () => api.entraGuests(connectionId),
    staleTime: 5 * 60 * 1000,
  });
  const [view, setView] = useState<"people" | "partners">("people");
  const [life, setLife] = useState<string>("");
  const [cls, setCls] = useState<string>("");
  const [dom, setDom] = useState<string>("");
  const [search, setSearch] = useState("");
  const [onlyEnabled, setOnlyEnabled] = useState(false);
  const [guestSort, setGuestSort] = useSortState<GuestKey>("guests", { key: "", dir: 1 });
  const [domainSort, setDomainSort] = useSortState<DomainKey>("guest-domains", { key: "", dir: 1 });

  const d = q.data;
  const filtered = useMemo(() => {
    const rows = d?.guests ?? [];
    const needle = search.trim().toLowerCase();
    return rows.filter((r) =>
      (!life || r.lifecycle === life)
      && (!cls || r.domain_class === cls)
      && (!dom || r.domain === dom)
      && (!onlyEnabled || r.enabled)
      && (!needle
          || r.display_name.toLowerCase().includes(needle)
          || r.upn.toLowerCase().includes(needle)
          || r.mail.toLowerCase().includes(needle)
          || r.domain.includes(needle)));
  }, [d?.guests, life, cls, dom, onlyEnabled, search]);

  const sortedGuests = useEntraSorted(filtered, guestSort, guestCmp);
  const sortedDomains = useEntraSorted(d?.domains ?? [], domainSort, domainCmp);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading guests…</div>;
  if (q.error) return <div className="p-6 text-sm text-red-700">{formatError(q.error)}</div>;
  if (!d) return <div className="p-6 text-sm text-gray-500">No data.</div>;
  if (d.counts.invited === 0) {
    return (
      <div className="p-6">
        <CoverageBanner meta={d.meta} />
        <EntraEmpty kind="clean" detail="This tenant has no external (B2B) users." />
      </div>
    );
  }

  const c = d.counts;
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <CoverageBanner meta={d.meta} />

      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-7">
        <Tile label="Guests" value={c.invited} />
        <Tile label="Pending invite" value={c.pending} tone="text-amber-600"
              title="Invited and never accepted." />
        <Tile label="Never used" value={c.never_used} tone="text-orange-600"
              title="Accepted the invitation but has never signed in." />
        <Tile label="Dormant" value={c.dormant} tone="text-rose-600"
              title={`No sign-in for ${d.stale_days} days or more.`} />
        <Tile label="Active" value={c.active} tone="text-emerald-600" />
        <Tile label="Not measured" value={c.not_measured} tone="text-gray-400"
              title="Sign-in activity was not collected — NOT evidence the account is unused." />
        <Tile label="Partner domains" value={d.domain_count} tone="text-sky-600" />
      </div>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Funnel c={c} />
        <div className="rounded-lg border bg-white p-3">
          <div className="mb-2 text-xs font-semibold text-gray-800">Where guests come from</div>
          <div className="space-y-1">
            {Object.entries(d.by_class).filter(([, n]) => n > 0).map(([k, n]) => (
              <div key={k} className="flex items-center gap-2 text-[11px]">
                <span className={`w-28 shrink-0 ${CLASS_META[k]?.cls || ""}`}>
                  {CLASS_META[k]?.label || k}
                </span>
                <div className="h-3 min-w-0 flex-1 rounded bg-gray-100">
                  <div className="h-3 rounded bg-brand/60"
                       style={{ width: `${(n / Math.max(1, c.invited)) * 100}%` }} />
                </div>
                <span className="w-12 shrink-0 text-right tabular-nums text-gray-700">
                  {n.toLocaleString()}
                </span>
              </div>
            ))}
          </div>
          {(d.by_class.consumer ?? 0) > 0 && (
            <div className="mt-2 text-[11px] text-rose-700">
              {d.by_class.consumer.toLocaleString()} guest(s) use a consumer mailbox. No partner
              organization can de-provision those when an engagement ends.
            </div>
          )}
        </div>
      </div>

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className="flex overflow-hidden rounded-md border text-xs">
          <button onClick={() => setView("people")}
                  className={`px-2.5 py-1 ${view === "people" ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
            People ({d.counts.invited.toLocaleString()})
          </button>
          <button onClick={() => setView("partners")}
                  className={`border-l px-2.5 py-1 ${view === "partners" ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
            Partner organizations ({d.domain_count.toLocaleString()})
          </button>
        </div>

        {view === "people" && (
          <>
            <input value={search} onChange={(e) => setSearch(e.target.value)}
                   placeholder="Search name, address or domain…"
                   className="w-56 rounded border px-2 py-1 text-xs" />
            <select value={life} onChange={(e) => setLife(e.target.value)}
                    aria-label="Lifecycle" className="rounded border px-2 py-1 text-xs">
              <option value="">All lifecycle states</option>
              {Object.entries(LIFECYCLE_META).map(([k, m]) => (
                <option key={k} value={k}>{m.label}</option>
              ))}
            </select>
            <select value={cls} onChange={(e) => setCls(e.target.value)}
                    aria-label="Domain class" className="rounded border px-2 py-1 text-xs">
              <option value="">All domain classes</option>
              {Object.entries(CLASS_META).map(([k, m]) => <option key={k} value={k}>{m.label}</option>)}
            </select>
            <label className="flex items-center gap-1 text-xs text-gray-600">
              <input type="checkbox" checked={onlyEnabled} onChange={(e) => setOnlyEnabled(e.target.checked)} />
              Enabled only
            </label>
            {dom && (
              <button onClick={() => setDom("")}
                      className="rounded border px-2 py-1 text-xs text-gray-700 hover:bg-gray-50">
                {dom} ✕
              </button>
            )}
          </>
        )}
      </div>

      {view === "people" ? (
        <div className="overflow-auto rounded-lg border bg-white">
          <table className="min-w-full text-xs">
            <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
              <tr>
                <SortTh label="Guest" col="name" sort={guestSort} setSort={setGuestSort} firstDir={1} />
                <SortTh label="Organization" col="domain" sort={guestSort} setSort={setGuestSort} firstDir={1} />
                <SortTh label="State" col="lifecycle" sort={guestSort} setSort={setGuestSort} firstDir={1} />
                <SortTh label="Invited" col="invited" sort={guestSort} setSort={setGuestSort} />
                <SortTh label="Last human sign-in" col="human" sort={guestSort} setSort={setGuestSort}
                        title="Interactive sign-in only — the one that evidences a person" />
                <SortTh label="Last any activity" col="any" sort={guestSort} setSort={setGuestSort}
                        title="Includes non-interactive token refresh" />
                <SortTh label="Account" col="enabled" sort={guestSort} setSort={setGuestSort} firstDir={1} />
              </tr>
            </thead>
            <tbody>
              {sortedGuests.slice(0, 1000).map((r) => {
                const m = LIFECYCLE_META[r.lifecycle] || LIFECYCLE_META.unknown;
                return (
                  <tr key={r.id} className="border-t">
                    <td className="px-2 py-1.5">
                      <div className="flex items-center gap-1">
                        <span className="min-w-0 truncate font-medium text-gray-900">
                          {r.display_name || r.upn}
                        </span>
                        <InvestigateLink principalId={r.id} />
                      </div>
                      <div className="truncate text-[11px] text-gray-500">{r.mail || r.upn}</div>
                    </td>
                    <td className="px-2 py-1.5">
                      <button onClick={() => { setDom(r.domain); }}
                              className="text-gray-800 hover:text-brand hover:underline">
                        {r.domain || "—"}
                      </button>
                      <div className={`text-[11px] ${CLASS_META[r.domain_class]?.cls || ""}`}>
                        {CLASS_META[r.domain_class]?.label || r.domain_class}
                      </div>
                    </td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded px-1.5 py-0.5 ${m.cls}`} title={m.why}>{m.label}</span>
                    </td>
                    <td className="px-2 py-1.5 tabular-nums text-gray-700" title={r.invited_at}>
                      {days(r.invited_days_ago)}
                    </td>
                    {/* Interactive only. Kept apart from the combined column on purpose. */}
                    <td className="px-2 py-1.5 tabular-nums text-gray-700"
                        title={r.signin_known ? (r.last_human_signin || "never") : "not measured"}>
                      {r.signin_known ? (r.last_human_signin ? days(r.last_human_days_ago) : "never") : "—"}
                    </td>
                    <td className="px-2 py-1.5 tabular-nums text-gray-500"
                        title="Includes non-interactive token refresh — live, but not necessarily a person.">
                      {r.signin_known ? (r.last_any_signin ? days(r.last_any_days_ago) : "never") : "—"}
                    </td>
                    <td className="px-2 py-1.5">
                      {r.enabled
                        ? <span className="text-gray-600">Enabled</span>
                        : <span className="text-amber-700">Disabled</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {sortedGuests.length > 1000 && (
            <div className="border-t px-2 py-1.5 text-[11px] text-gray-500">
              Showing the first 1,000 of {sortedGuests.length.toLocaleString()} matching guests —
              use the filters, or export for the full set.
            </div>
          )}
          {sortedGuests.length === 0 && (
            <div className="px-2 py-3 text-[11px] text-gray-500">No guest matches these filters.</div>
          )}
        </div>
      ) : (
        <div className="overflow-auto rounded-lg border bg-white">
          {!d.cross_tenant_known && (
            <div className="border-b bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
              The cross-tenant partner list could not be read, so governance shows as
              <strong> unknown</strong> rather than ungoverned.
            </div>
          )}
          <table className="min-w-full text-xs">
            <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
              <tr>
                <SortTh label="Organization" col="domain" sort={domainSort} setSort={setDomainSort} firstDir={1} />
                <SortTh label="Guests" col="guests" sort={domainSort} setSort={setDomainSort} />
                <SortTh label="Pending" col="pending" sort={domainSort} setSort={setDomainSort} />
                <SortTh label="Dormant" col="dormant" sort={domainSort} setSort={setDomainSort} />
                <SortTh label="Oldest invite" col="oldest" sort={domainSort} setSort={setDomainSort} />
                <SortTh label="Cross-tenant policy" col="governance" sort={domainSort} setSort={setDomainSort} firstDir={1} />
              </tr>
            </thead>
            <tbody>
              {sortedDomains.map((r) => {
                const g = GOV_META[r.governance || "unknown"] || GOV_META.unknown;
                return (
                  <tr key={r.domain} className="border-t">
                    <td className="px-2 py-1.5">
                      <button onClick={() => { setDom(r.domain); setView("people"); }}
                              className="font-medium text-gray-900 hover:text-brand hover:underline">
                        {r.partner_name || r.domain}
                      </button>
                      <div className={`text-[11px] ${CLASS_META[r.domain_class]?.cls || ""}`}>
                        {r.partner_name ? `${r.domain} · ` : ""}
                        {CLASS_META[r.domain_class]?.label || r.domain_class}
                      </div>
                    </td>
                    <td className="px-2 py-1.5 tabular-nums text-gray-900">
                      {r.guests.toLocaleString()}
                      {r.disabled > 0 && (
                        <span className="ml-1 text-[11px] text-gray-500">({r.disabled} disabled)</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 tabular-nums text-amber-700">{r.pending || ""}</td>
                    <td className="px-2 py-1.5 tabular-nums text-rose-700">{r.dormant || ""}</td>
                    <td className="px-2 py-1.5 tabular-nums text-gray-700">{days(r.oldest_invite_days)}</td>
                    <td className={`px-2 py-1.5 ${g.cls}`} title={r.governance_reason || ""}>{g.label}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
