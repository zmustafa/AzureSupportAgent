import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type EntraDomainMeta, type EntraPermissionRecheck } from "../../api";
import { formatError } from "../../utils/format";
import {
  EntraEmpty, StateChip, domainNote, BlockerList,
  DOMAIN_STATE_RANK, SortTh, cmp, useEntraSorted, useSortState, type SortState,
} from "./EntraShared";
import { IdentityFabricCard } from "./EntraIdentityFabric";

// ---------------------------------------------------------------------------- sorting
// "none" is the untouched state. `meta.domains` arrives as an object whose key order is the
// collector's own, and this table must open showing exactly that.
type DomainSortKey = "none" | "domain" | "state" | "items";
const DOMAIN_SORT_DEFAULT: SortState<DomainSortKey> = { key: "none", dir: -1 };

function compareDomain(a: EntraDomainMeta, b: EntraDomainMeta, key: DomainSortKey): number {
  switch (key) {
    case "domain": return cmp.text(a.name, b.name);
    // Ranked, not alphabetical, and ranked worst-highest so one descending click puts the
    // errored and not-permitted domains — the only rows worth acting on — at the top.
    case "state": return cmp.rank(DOMAIN_STATE_RANK, a.status, b.status);
    // A domain that never reported an item count has not collected zero items; cmp.num
    // treats the absence as unknown and sinks it either way.
    case "items": return cmp.num(a.item_count, b.item_count);
    default: return 0;
  }
}

/**
 * Setup & coverage.
 *
 * The consent set this product needs is powerful read access, and asking for all of it on
 * day one is a deal-breaker in most enterprises. This screen makes the tiers explicit, shows
 * what each unlocks, and turns granting the next tier into a visible coverage win.
 */
export function EntraSetupView({ connectionId }: { connectionId: string | null }) {
  const qc = useQueryClient();
  const [recheck, setRecheck] = useState<EntraPermissionRecheck | null>(null);
  const [recheckError, setRecheckError] = useState("");
  const setupQ = useQuery({
    queryKey: ["entra-setup", connectionId],
    queryFn: () => api.entraSetup(connectionId),
  });
  const diagQ = useQuery({
    queryKey: ["entra-diagnostics", connectionId],
    queryFn: () => api.entraDiagnostics(connectionId),
  });

  // Every other number on this screen describes the permissions held WHEN THE SNAPSHOT WAS
  // TAKEN. Without this, granting a scope and coming back shows no change at all, and there
  // is no way to tell "you have not granted it" from "we have not looked since you did".
  const recheckM = useMutation({
    mutationFn: () => api.entraRecheckPermissions(connectionId),
    onSuccess: (res) => {
      setRecheck(res);
      setRecheckError("");
      void qc.invalidateQueries({ queryKey: ["entra-setup"] });
      void qc.invalidateQueries({ queryKey: ["entra-status"] });
    },
    onError: (e) => { setRecheck(null); setRecheckError(formatError(e)); },
  });

  if (setupQ.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  if (setupQ.isError) return <div className="p-6 text-sm text-red-600">{formatError(setupQ.error)}</div>;
  const data = setupQ.data!;
  const meta = data.meta;
  const graph = (diagQ.data?.graph ?? {}) as Record<string, number>;

  return (
    <div className="space-y-4 p-4">
      {/* What is blocking coverage, once each ------------------------------------- */}
      {(meta.blockers ?? []).length > 0 && (
        <Card title="What is limiting coverage">
          <BlockerList blockers={meta.blockers ?? []} />
          {/* Consent only grants what the app manifest already requests. Sending someone to
              the consent link before they have added the permission produces no change and
              looks like the product ignoring them, so the order is stated explicitly. */}
          {data.app_registration?.client_id && (
            <div className="mt-2 rounded border bg-gray-50 px-2 py-1.5 text-[12px] text-gray-700">
              <div>
                To grant a permission: add it to app registration{" "}
                <code className="rounded bg-white px-1">{data.app_registration.client_id}</code>{" "}
                as an <span className="font-medium">Application</span> permission (not
                Delegated — an app-only token never carries delegated scopes), then grant
                admin consent.
              </div>
              <div className="mt-1 flex flex-wrap gap-3">
                {data.app_registration.portal_url && (
                  <a
                    href={data.app_registration.portal_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium underline underline-offset-2"
                  >
                    1. Open API permissions →
                  </a>
                )}
                {data.consent_url && (
                  <a
                    href={data.consent_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium underline underline-offset-2"
                  >
                    2. Grant admin consent →
                  </a>
                )}
              </div>
            </div>
          )}
        </Card>
      )}

      {/* How the tenant authenticates ------------------------------------------- */}
      <IdentityFabricCard fabric={setupQ.data?.identity_fabric} />

      {/* Token + license state -------------------------------------------------- */}
      <div className="grid gap-3 md:grid-cols-2">
        <Card title="Microsoft Graph access">
          <Row label="Token">
            {meta.permissions_summary.token_ok === true ? (
              <span className="rounded bg-green-100 px-1.5 py-0.5 text-[11px] font-medium text-green-700">
                acquired
              </span>
            ) : meta.permissions_summary.token_ok === false ? (
              <span className="text-red-600">{meta.permissions_summary.token_error || "unavailable"}</span>
            ) : (
              <span className="text-gray-400">unknown — run a refresh</span>
            )}
          </Row>
          <Row label="Permissions granted">
            {data.granted_known ? `${data.granted.length} application permission(s)` : "unknown"}
          </Row>
          {!data.granted_known && data.claim_error && (
            <div className="mt-1 text-xs text-gray-500">{data.claim_error}</div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              onClick={() => recheckM.mutate()}
              disabled={recheckM.isPending}
              className="rounded border bg-white px-2 py-1 text-[12px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {recheckM.isPending ? "Checking with Microsoft…" : "Re-check permissions now"}
            </button>
            <span className="text-[11px] text-gray-500">
              Reads consent live. Everything else on this page is as of the last refresh.
            </span>
          </div>
          {recheckError && <div className="mt-1 text-[12px] text-red-600">{recheckError}</div>}
          {recheck && <RecheckResult result={recheck} />}
        </Card>
        <Card title="License tier">
          <div className="flex flex-wrap gap-2">
            {(["p1", "p2", "governance", "workload_id_premium"] as const).map((k) => (
              <span
                key={k}
                title={data.licence_value?.[k] ?? ""}
                className={`rounded px-2 py-0.5 text-xs font-medium ${
                  meta.licences?.[k] ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
                }`}
              >
                {k === "p1" ? "Entra ID P1" : k === "p2" ? "Entra ID P2" : k === "governance" ? "ID Governance" : "Workload Identities"}
              </span>
            ))}
          </div>
          {!meta.licences?.detected && (
            <div className="mt-2 text-xs text-gray-500">
              {meta.licences?.reason || "License tier not detected — checks are still attempted."}
            </div>
          )}
        </Card>
      </div>

      {/* Consent tiers ---------------------------------------------------------- */}
      <div className="space-y-3">
        {data.tiers.map((t) => (
          <div key={t.tier} className="rounded-lg border bg-white p-3">
            <div className="flex items-center gap-2">
              <span className="text-[13px] font-semibold text-gray-900">
                Tier {t.tier} — {t.name}
              </span>
              {t.complete ? <StateChip state="ok" title="Fully granted" /> : <StateChip state="partial" />}
              <span className="ml-auto text-xs text-gray-500">
                {t.granted.length}/{t.scopes.length} granted
              </span>
            </div>
            <div className="mt-1 text-xs text-gray-600">{t.unlocks}</div>
            <div className="mt-2 flex flex-wrap gap-1">
              {t.scopes.map((s) => {
                const held = t.granted.includes(s);
                return (
                  <code
                    key={s}
                    className={`rounded px-1.5 py-0.5 text-[11px] ${
                      held ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-500 line-through decoration-gray-300"
                    }`}
                  >
                    {s}
                  </code>
                );
              })}
            </div>
          </div>
        ))}
        <div className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs text-sky-900">
          Every scope above is <strong>read-only</strong>. This product never requests a
          <code className="mx-1 rounded bg-sky-100 px-1">ReadWrite</code> permission and never writes to the directory.
        </div>
      </div>

      {/* Per-domain state -------------------------------------------------------- */}
      <Card title="Collector coverage">
        {!meta.loaded ? (
          <EntraEmpty kind="cold" detail="No collection has run for this tenant yet." />
        ) : (
          <DomainCoverageTable domains={Object.values(meta.domains ?? {})} />
        )}
      </Card>

      {/* Graph instrumentation ---------------------------------------------------- */}
      {Object.keys(graph).length > 0 && (
        <Card title="Last collection — Microsoft Graph">
          <div className="grid grid-cols-2 gap-2 text-[13px] md:grid-cols-4">
            <Stat label="Requests" value={graph.requests} />
            <Stat label="Batches" value={graph.batches} />
            <Stat label="Throttle events" value={graph.throttled} />
            <Stat label="Retries" value={graph.retries} />
            <Stat label="Pages" value={graph.pages} />
            <Stat label="Items" value={graph.items} />
            <Stat label="Forbidden" value={graph.forbidden} />
            <Stat label="Elapsed (ms)" value={graph.ms} />
          </div>
        </Card>
      )}
    </div>
  );
}

/**
 * Per-domain collection state.
 *
 * Its own component so the sort hooks are not stranded behind the `meta.loaded` branch.
 * The Note column stays unsorted: it is generated prose, and ordering prose alphabetically
 * says nothing a reader wanted to know.
 */
function DomainCoverageTable({ domains }: { domains: EntraDomainMeta[] }) {
  const [sort, setSort] = useSortState<DomainSortKey>("setup-domain-coverage", DOMAIN_SORT_DEFAULT);
  const rows = useEntraSorted(domains, sort, compareDomain);
  return (
    <table className="w-full text-[13px]">
      <thead>
        <tr className="border-b text-left text-xs text-gray-500">
          <SortTh label="Domain" col="domain" sort={sort} setSort={setSort} firstDir={1} />
          <SortTh label="State" col="state" sort={sort} setSort={setSort}
                  title="Sort by collection state — descending puts errors and blind domains first" />
          <SortTh label="Items" col="items" sort={sort} setSort={setSort} />
          <th className="py-1.5 font-medium">Note</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.name} className="border-b last:border-b-0">
            <td className="py-1.5 font-medium text-gray-800">{d.name}</td>
            <td className="py-1.5">
              <StateChip state={d.status} />
            </td>
            <td className="py-1.5 text-gray-600">{d.item_count.toLocaleString()}</td>
            <td className="py-1.5 text-xs text-gray-500">{domainNote(d)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The outcome of a live consent check.
 *
 * States a permission and its data separately on purpose: holding a scope and having
 * collected what it unlocks are different facts, and merging them would let this panel
 * claim coverage the snapshot does not have.
 */
function RecheckResult({ result }: { result: EntraPermissionRecheck }) {
  const stillBlind = result.blind_domains;
  return (
    <div className="mt-2 space-y-1 rounded border bg-gray-50 px-2 py-1.5 text-[12px]">
      <div className="text-gray-800">
        <span className="font-medium">{result.granted.length}</span> permission(s) granted right now.
      </div>
      {result.gained.length > 0 && (
        <div className="text-green-700">
          Newly granted since the last refresh: {result.gained.join(", ")}
        </div>
      )}
      {result.revoked.length > 0 && (
        <div className="text-red-700">No longer granted: {result.revoked.join(", ")}</div>
      )}
      {result.gained.length === 0 && result.revoked.length === 0 && (
        <div className="text-gray-600">No change since the last refresh.</div>
      )}
      {stillBlind.length > 0 && (
        <div className="text-amber-800">
          Still not permitted: {stillBlind.join(", ")} — these need consent, not a refresh.
        </div>
      )}
      {result.licence_blocked.length > 0 && (
        <div className="text-amber-800">
          Licensed-blocked: {result.licence_blocked.join(", ")} — the permission is held, but
          the tenant license does not include it. Granting more consent will not change this.
        </div>
      )}
      {result.needs_refresh && (
        <div className="font-medium text-sky-800">
          Run a refresh to collect what the new permission unlocks — consent alone does not
          backfill the data.
        </div>
      )}
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 text-[13px] font-semibold text-gray-800">{title}</div>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 py-0.5 text-[13px]">
      <span className="w-44 shrink-0 text-gray-500">{label}</span>
      <span className="text-gray-800">{children}</span>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | undefined }) {
  return (
    <div className="rounded bg-gray-50 px-2 py-1.5">
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className="text-sm font-semibold text-gray-800">{(value ?? 0).toLocaleString()}</div>
    </div>
  );
}
