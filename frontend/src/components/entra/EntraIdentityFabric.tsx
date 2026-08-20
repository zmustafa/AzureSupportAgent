import { useState } from "react";
import type {
  EntraExternalIdp, EntraFabricBrief, EntraFabricCertificate, EntraFabricHybrid,
  EntraFabricTrust, EntraIdentityFabric,
} from "../../api";
import { StateChip } from "./EntraShared";

/**
 * The tenant's authentication perimeter.
 *
 * A federated domain means Entra is not the authenticator — somebody else is, and Entra
 * takes their word for it. That single fact decides whether the rest of this product's
 * authentication numbers can be read at face value, so it belongs next to the consent
 * tiers rather than buried in a findings list.
 */
export type FabricCertificate = EntraFabricCertificate;
export type FabricTrust = EntraFabricTrust;
export type FabricHybrid = EntraFabricHybrid;
export type IdentityFabric = EntraIdentityFabric;
export type ExternalIdp = EntraExternalIdp;
export type FabricBrief = EntraFabricBrief;

function Fact({ label, value, tone = "", title }: {
  label: string; value: React.ReactNode; tone?: string; title?: string;
}) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`truncate text-[12px] ${tone || "text-gray-800"}`}>{value}</div>
    </div>
  );
}

function Copyable({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  if (!value) return <span className="text-gray-400">—</span>;
  return (
    <button
      onClick={() => { void navigator.clipboard?.writeText(value).then(() => setDone(true)); }}
      title={`${value}\n\nClick to copy`}
      className="max-w-full truncate text-left font-mono text-[11px] text-gray-700 underline decoration-dotted underline-offset-2 hover:text-brand"
    >
      {done ? "copied" : value}
    </button>
  );
}

function certTone(cert?: FabricCertificate): string {
  if (!cert?.parsed || cert.days_left == null) return "text-gray-500";
  if (cert.expired) return "text-red-600";
  if (cert.days_left <= 30) return "text-red-600";
  if (cert.days_left <= 60) return "text-amber-700";
  return "text-gray-800";
}

function certText(cert?: FabricCertificate): string {
  if (!cert) return "—";
  if (cert.parsed === false) return "could not be parsed";
  if (!cert.parsed) return "—";
  const when = (cert.not_after || "").slice(0, 10);
  if (cert.expired) return `expired ${when} (${Math.abs(cert.days_left ?? 0)} days ago)`;
  return `${when} · ${cert.days_left} days left`;
}

function TrustRow({ trust }: { trust: FabricTrust }) {
  const [open, setOpen] = useState(false);
  const vendor = trust.vendor?.label || "Unrecognized provider";
  const share = trust.user_share != null ? `${Math.round(trust.user_share * 100)}%` : null;
  return (
    <div className="border-t first:border-t-0">
      <button onClick={() => setOpen((v) => !v)}
              className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-gray-50">
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
        <span className="font-medium text-gray-900">{trust.domain}</span>
        <span className="text-gray-400">→</span>
        <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[11px] font-medium text-violet-700">
          {vendor}
        </span>
        {trust.protocol && (
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] uppercase text-gray-600">
            {trust.protocol}
          </span>
        )}
        {trust.user_count != null && (
          <span className="text-[11px] text-gray-500">
            {trust.user_count.toLocaleString()} user(s){share ? ` · ${share} of the directory` : ""}
          </span>
        )}
        {trust.mfa_behaviour?.trusted && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800"
                title={trust.mfa_behaviour.label}>
            MFA claim trusted
          </span>
        )}
        {trust.auto_rollover && !trust.auto_rollover.healthy && trust.auto_rollover.result && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800"
                title={`Automatic certificate rollover reports ${trust.auto_rollover.result}`}>
            rollover: {trust.auto_rollover.result}
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-3 bg-gray-50 px-3 py-3">
          <div className="grid gap-2 md:grid-cols-3">
            <Fact label="Issuer URI" value={<Copyable value={trust.issuer_uri || ""} />} />
            <Fact label="Provider host" value={<Copyable value={trust.host || ""} />} />
            <Fact label="Protocol" value={trust.protocol || "—"} />
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <Fact label="Sign-in endpoint (passive)" value={<Copyable value={trust.passive_sign_in_uri || ""} />} />
            <Fact label="Sign-in endpoint (active)" value={<Copyable value={trust.active_sign_in_uri || ""} />} />
            <Fact label="Sign-out endpoint" value={<Copyable value={trust.sign_out_uri || ""} />} />
            <Fact label="Metadata exchange" value={<Copyable value={trust.metadata_exchange_uri || ""} />} />
          </div>
          <div className="grid gap-2 md:grid-cols-3">
            <Fact
              label="MFA claim"
              tone={trust.mfa_behaviour?.trusted ? "text-amber-700" : "text-gray-800"}
              title={trust.mfa_behaviour?.explicit
                ? "Explicitly configured on the domain."
                : "Not set, so Entra applies the permissive default."}
              value={trust.mfa_behaviour
                ? `${trust.mfa_behaviour.label}${trust.mfa_behaviour.explicit ? "" : " (default)"}`
                : "—"}
            />
            <Fact label="Signed request required"
                  value={trust.signed_request_required == null
                    ? "not set"
                    : trust.signed_request_required ? "yes" : "no"} />
            <Fact label="Prompt login behavior" value={trust.prompt_login_behavior || "not set"} />
          </div>
          {/* Thumbprint, subject and expiry only. The certificate itself is parsed in the
              collector and discarded — the same rule application credentials follow. */}
          <div className="grid gap-2 md:grid-cols-2">
            <Fact label="Signing certificate" tone={certTone(trust.certificate)}
                  value={certText(trust.certificate)}
                  title={trust.certificate?.subject} />
            <Fact label="Successor certificate" tone={certTone(trust.next_certificate)}
                  value={certText(trust.next_certificate)}
                  title={trust.next_certificate?.subject} />
            <Fact label="Certificate thumbprint"
                  value={<Copyable value={trust.certificate?.thumbprint || ""} />} />
            <Fact label="Automatic rollover"
                  tone={trust.auto_rollover?.healthy ? "text-gray-800" : "text-amber-700"}
                  value={trust.auto_rollover?.result
                    ? `${trust.auto_rollover.result}${trust.auto_rollover.last_run
                        ? ` · last run ${trust.auto_rollover.last_run.slice(0, 10)}` : ""}`
                    : "—"} />
          </div>
        </div>
      )}
    </div>
  );
}

function Flag({ on, label, warnWhenOff }: { on?: boolean; label: string; warnWhenOff?: boolean }) {
  const tone = on
    ? "bg-green-100 text-green-700"
    : warnWhenOff ? "bg-amber-100 text-amber-800" : "bg-gray-100 text-gray-500";
  return <span className={`rounded px-1.5 py-0.5 text-[11px] ${tone}`}>{label} {on ? "on" : "off"}</span>;
}

export function IdentityFabricCard({ fabric }: { fabric?: IdentityFabric }) {
  const trusts = fabric?.federation || [];
  const hybrid = fabric?.hybrid || {};
  // Three different "we cannot tell you" states, and they must not read alike. A snapshot
  // taken before this collector existed simply has no federation block — telling that
  // reader to check their permissions would send them hunting for a consent problem that
  // does not exist. `readable === false` is the real permission or Graph failure.
  const stale = !fabric || fabric.readable === undefined;
  const blind = !stale && fabric.readable === false;
  return (
    <div className="rounded-lg border bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <span className="text-[13px] font-semibold text-gray-800">Identity fabric</span>
        {stale ? (
          <StateChip state="stale" title="Collected before this check existed." />
        ) : blind ? (
          <StateChip state="blind" title={fabric?.blind_reason || "The domain list could not be read."} />
        ) : (
          <>
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
              {fabric?.managed_count ?? 0} managed
            </span>
            <span className={`rounded px-1.5 py-0.5 text-[11px] ${
              trusts.length ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-600"}`}>
              {fabric?.federated_count ?? 0} federated
            </span>
          </>
        )}
        <span className="ml-auto text-[11px] text-gray-500">{fabric?.summary}</span>
      </div>

      {stale ? (
        <div className="px-4 py-3 text-[12px] text-gray-600">
          This snapshot was collected before the authentication perimeter was read. Refresh the
          tenant to see whether any domain is federated to an external identity provider.
        </div>
      ) : blind ? (
        <div className="px-4 py-3 text-[12px] text-gray-600">
          The tenant's authentication perimeter could not be read, so this screen cannot say
          whether any domain is federated. {fabric?.blind_reason}
        </div>
      ) : trusts.length === 0 ? (
        // A cloud-only tenant is the good outcome. It gets one calm sentence, not an empty
        // table implying something is missing.
        <div className="px-4 py-3 text-[12px] text-gray-600">
          Every verified domain authenticates in Entra ID. No external identity provider is
          federated, so the authentication figures elsewhere in this view describe the whole
          directory.
        </div>
      ) : (
        <div>{trusts.map((t) => <TrustRow key={t.domain} trust={t} />)}</div>
      )}

      {!stale && (
      <>
      {/* Guests are a second, separate perimeter: a tenant can authenticate all its own
          staff in the cloud and still accept Google or a partner's SAML for externals. */}
      <div className="border-t px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-gray-400">Guest sign-in</span>
          {fabric?.external_idps_readable ? (
            (fabric.external_idps || []).length === 0 ? (
              <span className="text-[12px] text-gray-600">
                No external identity provider is configured for guests.
              </span>
            ) : (
              (fabric.external_idps || []).map((idp) => (
                <span key={idp.id}
                      title={[idp.kind_label, idp.issuer_uri, idp.domain, idp.client_id ? `client ${idp.client_id}` : ""]
                        .filter(Boolean).join(" · ")}
                      className="rounded bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-800">
                  {idp.display_name || idp.identity_provider_type || idp.id}
                </span>
              ))
            )
          ) : (
            <span className="text-[11px] text-amber-700">
              Not readable — needs {fabric?.external_idps_reason || "IdentityProvider.Read.All"}.
              Guests may sign in with a provider this screen cannot see.
            </span>
          )}
        </div>
      </div>

      <div className="border-t px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-gray-400">Hybrid</span>
          <Flag on={hybrid.sync_enabled} label="directory sync" />
          {hybrid.sync_enabled && hybrid.last_sync && (
            <span className="text-[11px] text-gray-500">last sync {hybrid.last_sync.slice(0, 16).replace("T", " ")}</span>
          )}
          {hybrid.features_readable ? (
            <>
              <Flag on={hybrid.password_sync} label="password hash sync" warnWhenOff={trusts.length > 0} />
              <Flag on={hybrid.password_writeback} label="password writeback" />
              <Flag on={hybrid.user_writeback} label="user writeback" />
              <Flag on={hybrid.group_writeback} label="group writeback" />
              {hybrid.deletion_prevention?.type && (
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
                  deletion prevention {hybrid.deletion_prevention.threshold ?? ""}
                </span>
              )}
            </>
          ) : hybrid.sync_enabled ? (
            <span className="text-[11px] text-amber-700">
              Sync configuration not readable — needs OnPremDirectorySynchronization.Read.All.
            </span>
          ) : null}
        </div>
        {trusts.length > 0 && (
          <div className="mt-2 text-[11px] text-gray-500">
            Authentication policy, multi-factor and lockout behavior for federated users live
            with the provider. Multi-factor registration figures elsewhere in this view describe
            the cloud-authenticated population only.
            {hybrid.features_readable && !hybrid.password_sync
              && " With password hash synchronisation off there is also no fallback if the provider is unreachable, and leaked-credential detection cannot run for these users."}
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
}

/**
 * The one-line version, for screens whose own numbers are qualified by federation.
 *
 * Rendered as a banner rather than a footnote on purpose: a registration gap that is really
 * "we cannot see this provider's MFA" is the kind of thing a reader has to be told before
 * they read the chart, not after.
 */
export function FederationNote({ fabric, context }: {
  fabric?: FabricBrief;
  context: "auth-methods" | "inline";
}) {
  if (!fabric?.federated) return null;
  const vendors = (fabric.vendors || []).join(", ") || "an external identity provider";
  const users = fabric.user_count;
  const share = fabric.user_share != null ? ` (${Math.round(fabric.user_share * 100)}%)` : "";

  if (context === "inline") {
    return (
      <span className="text-[12px] text-violet-700" title={fabric.summary}>
        federated to {vendors}
        {users != null ? ` · ${users.toLocaleString()} user(s)${share}` : ""}
      </span>
    );
  }
  return (
    <div className="mb-3 rounded border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-900">
      <span className="font-semibold">
        {users != null ? `${users.toLocaleString()} user(s)${share} authenticate at ${vendors}.` : `This tenant federates to ${vendors}.`}
      </span>{" "}
      Their multi-factor authentication happens at the provider and is invisible to Entra, so it
      is not counted below. These figures describe the users who authenticate in the cloud, plus
      anyone who separately registered an Entra method.
    </div>
  );
}
