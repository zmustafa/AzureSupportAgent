import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type EntraActivationAction, type EntraActivationActionsResult,
         type EntraActivationSession } from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import {
  EntraEmpty, EntraTimeWindow, Segmented, SortTh, cmp, useEntraSorted, useSortState,
} from "./EntraShared";

/**
 * Privileged activation sessions — who elevated, when, under what terms, and what they did.
 *
 * The last part is the reason this screen exists. PIM records that an elevation happened and
 * the audit logs record what changed, and nothing in the Microsoft portals joins the two, so
 * "who turned on Global Administrator and what did they do with it" is a manual correlation
 * across two consoles. Here it is one row and a drawer.
 *
 * Actions are fetched per session on demand, never up front: the Azure Activity Log is
 * per-subscription and slow, and this tenant has 26 of them.
 */

const TIER_CHIP: Record<string, string> = {
  tier0: "bg-red-100 text-red-700",
  tier1: "bg-amber-100 text-amber-700",
  tier2: "bg-gray-100 text-gray-600",
};

const ATTRIBUTION: Record<string, { label: string; chip: string; hint: string }> = {
  required_activation: {
    label: "needed this elevation",
    chip: "bg-red-100 text-red-700",
    hint: "The principal holds no standing privileged role that covers this, so the "
        + "activation is what made it possible.",
  },
  possible_without: {
    label: "possible anyway",
    chip: "bg-gray-100 text-gray-600",
    hint: "A standing role already allowed this. The elevation happened to be open at the "
        + "time but was not required.",
  },
  unclassified: {
    label: "unclassified",
    chip: "bg-slate-100 text-slate-600",
    hint: "The standing-permission picture is unavailable, so no claim is made either way.",
  },
};

function Chip({ text, cls, title }: { text: string; cls: string; title?: string }) {
  return (
    <span title={title} className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
      {text}
    </span>
  );
}

/**
 * Sorting for the two grids on this screen.
 *
 * Both enum columns rank rather than spell: "missing" is not between "ok" and "weak" in any
 * sense a reader cares about, and `unknown` is a fourth state — the source cannot carry a
 * justification at all — which must not be read as "nobody gave a reason".
 */
type SessionKey = "started" | "who" | "role" | "where" | "for" | "reason";

const JUSTIFICATION_RANK: Record<string, number> = {
  missing: 3,  // no reason given: the interesting end of a descending sort
  weak: 2,
  ok: 1,
  unknown: 0,  // the source cannot record one — our blind spot, not their omission
};

const ATTRIBUTION_RANK: Record<string, number> = {
  required_activation: 2,  // the elevation is what made this possible
  possible_without: 1,
  unclassified: 0,
};

/** The "Where" cell as one string: plane first, exactly as the column reads. */
function whereText(s: EntraActivationSession): string {
  const scope = s.scope_name || (s.scope_type === "directory" ? "directory" : s.scope_type);
  return `${s.plane === "azure" ? "Azure" : "Entra"} ${scope}`;
}

function compareSession(a: EntraActivationSession, b: EntraActivationSession, key: SessionKey): number {
  switch (key) {
    case "started": return cmp.date(a.start, b.start);
    case "who": return cmp.text(a.label, b.label);
    case "role": return cmp.text(a.role_name || a.role_id, b.role_name || b.role_id);
    case "where": return cmp.text(whereText(a), whereText(b));
    // Granted duration, null when the request never provisioned.
    case "for": return cmp.num(a.granted_hours, b.granted_hours);
    case "reason":
      return cmp.rank(JUSTIFICATION_RANK, a.justification_quality, b.justification_quality);
  }
}

type ActionKey = "when" | "operation" | "target" | "attribution";

function compareAction(a: EntraActivationAction, b: EntraActivationAction, key: ActionKey): number {
  switch (key) {
    case "when": return cmp.date(a.at, b.at);
    case "operation": return cmp.text(a.operation, b.operation);
    case "target": return cmp.text(a.target, b.target);
    case "attribution": return cmp.rank(ATTRIBUTION_RANK, a.attribution, b.attribution);
  }
}

/** Stable identities for the pre-load render, so the sort memos survive a re-render. */
const NO_SESSIONS: EntraActivationSession[] = [];
const NO_ACTIONS: EntraActivationAction[] = [];

function when(iso: string): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

export function EntraActivationsView({ connectionId }: { connectionId: string | null }) {
  const [plane, setPlane] = useState("");
  const [tier, setTier] = useState("");
  const [days, setDays] = useState(90);
  const [search, setSearch] = useState("");
  const [only, setOnly] = useState("");
  const [selected, setSelected] = useState<EntraActivationSession | null>(null);
  // Brushed sub-window over the loaded sessions. Null means "the whole loaded range", which is
  // not the same as the `days` selector: that one decides what the server returns, this one
  // slices what came back without another round trip.
  const [range, setRange] = useState<[number, number] | null>(null);
  const q = useDebounced(search, 250);

  // Any change to what the server returns invalidates the brush — keeping it would silently
  // hide rows from the new result set behind a window the user picked for the old one.
  useEffect(() => { setRange(null); }, [connectionId, days, plane, tier, q, only]);

  // Activations are stamped in UTC. Judging "out of hours" against UTC in a tenant that
  // works in another zone is confidently wrong, so the browser's own offset is sent and
  // shown, rather than pretending the server knows the tenant's working day.
  const offsetHours = useMemo(() => -new Date().getTimezoneOffset() / 60, []);

  const query = useQuery({
    queryKey: ["entra-activations", connectionId, days, plane, tier, q, offsetHours],
    queryFn: () => api.entraActivations(
      { days, plane, tier, q, utcOffsetHours: offsetHours }, connectionId),
  });

  // Default is newest first, which is the order the server already returns.
  const [sort, setSort] = useSortState<SessionKey>("activation-sessions", { key: "started", dir: -1 });

  // Filter -> brush window -> sort, in that order and no other. Sorting is the last step so
  // it can only reorder what the window already admitted; sorting earlier would let a click
  // on a column heading pull rows back in from outside the brushed range.
  const sessions = query.data?.sessions ?? NO_SESSIONS;
  const rows = useMemo(() => sessions.filter((s) => {
    if (only === "tier0") return s.tier === "tier0";
    if (only === "out_of_hours") return s.in_business_hours === false;
    if (only === "no_justification") return s.justification_quality === "missing";
    if (only === "weak") return s.justification_quality === "weak";
    if (only === "attempts") return !s.granted;
    return true;
  }), [sessions, only]);
  const windowed = useMemo(() => (range
    ? rows.filter((s) => {
        const t = startMs(s);
        return t !== null && t >= range[0] && t <= range[1];
      })
    : rows), [rows, range]);
  const sorted = useEntraSorted(windowed, sort, compareSession);

  if (query.isLoading) {
    return <div className="p-6 text-sm text-gray-500">Loading activation sessions…</div>;
  }
  if (query.isError) {
    return <div className="p-6 text-sm text-red-600">{formatError(query.error)}</div>;
  }
  const d = query.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  const caps = d.capabilities || {};
  const facets = d.facets || {};

  return (
    <div className="space-y-3 p-4">
      <SourceBanner caps={caps} ledger={d.ledger} lookback={d.lookback_days} />

      <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
        {/* Sessions is the "no filter" tile, so it clears BOTH filters rather than only the
            one it happens to share state with. Clearing `only` while leaving the plane set
            left the grid filtered with nothing on screen claiming to be filtering it. */}
        <Tile label="Sessions" value={d.total}
              onClick={() => { setOnly(""); setPlane(""); }}
              active={!only && !plane}
              title="Every activation in the window, unfiltered" />
        {/* These two are the plane filter, which is also the segmented control below. One
            state, two ways in — a tile that showed a count and did nothing when clicked was
            the odd one out in a row of five that all filtered. */}
        <Tile label="Entra ID" value={facets.entra ?? 0}
              onClick={() => setPlane(plane === "entra" ? "" : "entra")}
              active={plane === "entra"}
              title="Directory role activations only" />
        <Tile label="Azure" value={facets.azure ?? 0}
              onClick={() => setPlane(plane === "azure" ? "" : "azure")}
              active={plane === "azure"}
              title="Azure resource activations only" />
        <Tile label="Tier-0" value={facets.tier0 ?? 0} tone="text-red-700"
              onClick={() => setOnly(only === "tier0" ? "" : "tier0")} active={only === "tier0"}
              title="Activations of the roles that can take over the tenant" />
        <Tile label="Out of hours" value={facets.out_of_hours ?? 0} tone="text-amber-700"
              onClick={() => setOnly(only === "out_of_hours" ? "" : "out_of_hours")}
              active={only === "out_of_hours"}
              title="Activations outside the working day in your timezone"
              note={offsetHours === 0 ? "UTC" : `UTC${offsetHours >= 0 ? "+" : ""}${offsetHours}`} />
        <Tile label="No reason given" value={facets.no_justification ?? 0} tone="text-amber-700"
              onClick={() => setOnly(only === "no_justification" ? "" : "no_justification")}
              active={only === "no_justification"}
              title="Activations recorded without a justification" />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search person, role, scope, reason…"
          className="w-64 rounded border px-2 py-1 text-[13px]"
        />
        {/* Buttons rather than a select: this is the switch a reviewer flips most often on
            this tab, and a dropdown hid two of the three planes behind a click. */}
        <Segmented
          label="Filter activations by plane"
          value={plane}
          onChange={setPlane}
          options={[
            { value: "", label: "Both planes", title: "Entra ID and Azure resource activations together" },
            { value: "entra", label: "Entra ID", title: "Directory role activations only" },
            { value: "azure", label: "Azure resources", title: "Azure RBAC activations only" },
          ]}
        />
        <select value={tier} onChange={(e) => setTier(e.target.value)}
                className="rounded border px-2 py-1 text-[13px]">
          <option value="">Any tier</option>
          <option value="tier0">Tier-0 only</option>
          <option value="tier1">Tier-1 only</option>
        </select>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
                className="rounded border px-2 py-1 text-[13px]">
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
          <option value={0}>Everything recorded</option>
        </select>
        {only && (
          <button onClick={() => setOnly("")}
                  className="text-[12px] text-brand underline underline-offset-2">
            clear filter
          </button>
        )}
        <span className="ml-auto text-[11px] text-gray-500">
          {windowed.length.toLocaleString()} shown
        </span>
      </div>

      <EntraTimeWindow
        label="Activation window"
        unit="activation"
        hotLabel="tier-0"
        points={rows.map((s) => ({ t: startMs(s) ?? NaN, hot: s.tier === "tier0" }))}
        value={range}
        onChange={setRange}
        shownCount={windowed.length}
      />
      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <SortTh label="Started" col="started" sort={sort} setSort={setSort} className="px-2" />
              <SortTh label="Who" col="who" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Role" col="role" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
              <SortTh label="Where" col="where" sort={sort} setSort={setSort} firstDir={1} className="px-2"
                      title="Sort by plane and scope" />
              <SortTh label="For" col="for" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by granted duration — never granted last" />
              <SortTh label="Reason" col="reason" sort={sort} setSort={setSort} className="px-2"
                      title="Sort by justification quality — no reason given first" />
              <th className="px-2 py-2 font-medium"> </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => (
              <tr key={s.id} className="border-t hover:bg-gray-50">
                <td className="px-2 py-1.5 whitespace-nowrap text-gray-600">
                  {when(s.start)}
                  {s.in_business_hours === false && (
                    <span className="ml-1" title="Outside the working day in your timezone">🌙</span>
                  )}
                </td>
                <td className="px-2 py-1.5">
                  <div className="truncate text-gray-900" title={s.label}>{s.label}</div>
                  {!s.granted && (
                    <Chip text={s.status || "not granted"} cls="bg-slate-100 text-slate-600"
                          title="This request never provisioned, so no privilege was issued." />
                  )}
                </td>
                <td className="px-2 py-1.5">
                  <span className="mr-1">
                    <Chip text={s.tier.replace("tier", "T")} cls={TIER_CHIP[s.tier] || TIER_CHIP.tier2} />
                  </span>
                  <span className="text-gray-800">{s.role_name || s.role_id}</span>
                </td>
                <td className="px-2 py-1.5 text-gray-600">
                  <span className="mr-1 text-[10px] uppercase text-gray-400">
                    {s.plane === "azure" ? "Azure" : "Entra"}
                  </span>
                  {s.scope_name || (s.scope_type === "directory" ? "directory" : s.scope_type)}
                </td>
                <td className="px-2 py-1.5 whitespace-nowrap text-gray-600">
                  {s.granted_hours == null ? "—" : `${s.granted_hours}h`}
                </td>
                <td className="px-2 py-1.5">
                  <Justification session={s} />
                </td>
                <td className="px-2 py-1.5 text-right">
                  <button onClick={() => setSelected(s)}
                          className="whitespace-nowrap text-[12px] text-brand underline underline-offset-2">
                    what they did
                  </button>
                </td>
              </tr>
            ))}
            {!windowed.length && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-sm text-gray-500">
                  {rows.length
                    ? "No activation in the selected window. Widen the slider or click All."
                    : "No activation matches these filters."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <SessionDrawer session={selected} connectionId={connectionId}
                       onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function Justification({ session }: { session: EntraActivationSession }) {
  if (session.justification_quality === "unknown") {
    return (
      <span className="text-[12px] italic text-gray-400"
            title="This source cannot record a justification — that is our blind spot, not a missing entry.">
        not recorded by this source
      </span>
    );
  }
  if (session.justification_quality === "missing") {
    return <Chip text="no reason given" cls="bg-amber-100 text-amber-800" />;
  }
  return (
    <span className={session.justification_quality === "weak" ? "text-amber-700" : "text-gray-700"}
          title={session.justification}>
      {session.justification.slice(0, 42)}
      {session.ticket_number && (
        <span className="ml-1 text-[11px] text-gray-500">#{session.ticket_number}</span>
      )}
    </span>
  );
}

function Tile({ label, value, tone, note, onClick, active, title }: {
  label: string; value: number; tone?: string; note?: string;
  onClick?: () => void; active?: boolean; title?: string;
}) {
  const Cmp = onClick ? "button" : "div";
  return (
    <Cmp onClick={onClick}
         title={title || (onClick ? `Filter to ${label.toLowerCase()}` : undefined)}
         aria-pressed={onClick ? Boolean(active) : undefined}
         className={`rounded-lg border bg-white p-2.5 text-left ${
           active ? "ring-2 ring-brand" : onClick ? "hover:bg-gray-50" : ""}`}>
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`text-lg font-semibold ${tone ?? "text-gray-900"}`}>
        {value.toLocaleString()}
      </div>
      {note && <div className="text-[10px] text-gray-400">{note}</div>}
    </Cmp>
  );
}

function SourceBanner({ caps, ledger, lookback }: {
  caps: Record<string, unknown>; ledger: Record<string, unknown>; lookback: number;
}) {
  const total = Number(ledger.total || 0);
  const earliest = String(ledger.earliest || "").slice(0, 10);
  return (
    <div className="space-y-2">
      {/* Keyed on `detail`, not on any single source. Justification comes from the PIM
          audit log, so keying this on `entra_requests` (which is permanently false for a
          read-only connection) made the banner claim the detail was missing while the
          table right below it displayed the reasons. */}
      {!caps.detail && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span className="font-medium">Entra activation detail is unavailable.</span>{" "}
          Windows, roles and people are exact, but the reason each elevation was requested is
          blank because the PIM audit log is not readable. Grant{" "}
          <code>AuditLog.Read.All</code>. Azure activations are unaffected — they read
          through Azure RBAC, not Graph.
        </div>
      )}
      {!caps.azure_requests && Boolean(caps.azure_reason) && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span className="font-medium">Azure activations are not included.</span>{" "}
          {String(caps.azure_reason)}
        </div>
      )}
      <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900">
        Every refresh appends to a local ledger, so this reaches further back than Microsoft
        keeps the data — Graph discards directory audits after 30 days.{" "}
        {total > 0
          ? <><strong>{total.toLocaleString()}</strong> session(s) retained
              {earliest && <> since {earliest}</>}.</>
          : <>Nothing retained yet.</>}
        {caps.azure_subscriptions ? ` ${caps.azure_subscriptions} Azure subscription(s) covered.` : ""}
        {lookback ? ` Each refresh collects the last ${lookback} days.` : ""}
      </div>
    </div>
  );
}

/** Session start as epoch ms, or null when the source did not stamp one. */
function startMs(s: EntraActivationSession): number | null {
  const t = new Date(s.start).getTime();
  return Number.isNaN(t) ? null : t;
}

function SessionDrawer({ session, connectionId, onClose }: {
  session: EntraActivationSession; connectionId: string | null; onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["entra-activation-actions", session.id, connectionId],
    queryFn: () => api.entraActivationActions(session.id, connectionId),
  });

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="flex h-full w-[46rem] max-w-full flex-col bg-white shadow-xl"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between border-b px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-gray-900">{session.label}</div>
            <div className="text-[12px] text-gray-500">
              {session.role_name || session.role_id} ·{" "}
              {session.plane === "azure" ? "Azure" : "Entra ID"} ·{" "}
              {session.scope_name || session.scope_type}
            </div>
          </div>
          <button onClick={onClose} className="text-sm text-gray-400 hover:text-gray-700">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <div className="mb-3 grid grid-cols-2 gap-2 text-[12px] md:grid-cols-4">
            <Fact label="Started" value={when(session.start)} />
            <Fact label="Ended" value={when(session.end)} />
            <Fact label="Granted for"
                  value={session.granted_hours == null ? "—" : `${session.granted_hours}h`} />
            <Fact label="Outcome" value={session.granted ? (session.status || "granted")
                                                         : (session.status || "not granted")} />
          </div>

          <div className="mb-3 rounded border bg-gray-50 p-2 text-[12px]">
            <div className="text-[11px] uppercase tracking-wide text-gray-400">Reason given</div>
            {session.justification_quality === "unknown"
              ? <div className="italic text-gray-500">
                  Not recorded by this source. Entra activations older than 30 days lose
                  their reason — Microsoft discards the PIM audit log after that, and only
                  the window survives in the ledger.
                </div>
              : <div className="text-gray-800">
                  {session.justification || <span className="text-amber-700">none given</span>}
                  {session.ticket_number && <span className="ml-2 text-gray-500">
                    ticket #{session.ticket_number}</span>}
                </div>}
          </div>

          {q.isLoading && (
            <div className="rounded border bg-white p-4 text-sm text-gray-500">
              Reading the audit trail for this window…
            </div>
          )}
          {q.isError && (
            <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {formatError(q.error)}
            </div>
          )}
          {q.data && <Actions data={q.data} />}
        </div>
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border bg-white p-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className="text-gray-800">{value}</div>
    </div>
  );
}

function Actions({ data }: { data: EntraActivationActionsResult }) {
  const counts = data.counts || {};
  const actions = data.actions || NO_ACTIONS;
  // Default is oldest first, the order the audit trail already arrives in — a window of
  // activity reads as a sequence.
  const [sort, setSort] = useSortState<ActionKey>("activation-actions", { key: "when", dir: 1 });
  const sorted = useEntraSorted(actions, sort, compareAction);
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[12px]">
        <span className="font-medium text-gray-800">
          {(counts.total ?? 0).toLocaleString()} action(s) during this window
        </span>
        {Boolean(counts.required_activation) && (
          <Chip text={`${counts.required_activation} needed this elevation`}
                cls={ATTRIBUTION.required_activation.chip}
                title={ATTRIBUTION.required_activation.hint} />
        )}
        {Boolean(counts.possible_without) && (
          <Chip text={`${counts.possible_without} possible anyway`}
                cls={ATTRIBUTION.possible_without.chip}
                title={ATTRIBUTION.possible_without.hint} />
        )}
        {Boolean(counts.unclassified) && (
          <Chip text={`${counts.unclassified} unclassified`}
                cls={ATTRIBUTION.unclassified.chip} title={ATTRIBUTION.unclassified.hint} />
        )}
        {data.cached && <span className="text-[11px] text-gray-400">cached</span>}
      </div>

      {/* The honest caveat. Without it this screen reads as an accusation. */}
      <div className="mb-2 rounded border border-gray-200 bg-gray-50 px-2 py-1.5 text-[11px] text-gray-600">
        Actions are what this principal did between{" "}
        {when(data.window?.start)} and {when(data.window?.end)} (±{data.window?.pad_minutes}min).
        {data.standing_entra_roles?.length
          ? ` They also hold ${data.standing_entra_roles.join(", ")} permanently, so some of
             this needed no elevation at all.`
          : " They hold no standing privileged directory role, so privileged actions here"
            + " required the elevation."}
      </div>

      {(data.notes || []).map((n: string, i: number) => (
        <div key={i} className="mb-1 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-900">
          {n}
        </div>
      ))}
      {!actions.length && (
        <div className="rounded border border-green-200 bg-green-50 p-3 text-[13px] text-green-900">
          Nothing was recorded during this window. The role was activated and then went unused —
          worth asking whether the elevation was needed.
        </div>
      )}

      {actions.length > 0 && (
        <div className="overflow-hidden rounded border">
          <table className="w-full text-[12px]">
            <thead className="bg-gray-50 text-left text-[10px] uppercase tracking-wide text-gray-500">
              <tr>
                <SortTh label="When" col="when" sort={sort} setSort={setSort} className="px-2" />
                <SortTh label="Operation" col="operation" sort={sort} setSort={setSort}
                        firstDir={1} className="px-2" />
                <SortTh label="Target" col="target" sort={sort} setSort={setSort}
                        firstDir={1} className="px-2" />
                <SortTh label="Attribution" col="attribution" sort={sort} setSort={setSort}
                        className="px-2" title="Sort by attribution — actions that needed this elevation first" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((a: EntraActivationAction, i: number) => (
                <tr key={i} className="border-t">
                  <td className="whitespace-nowrap px-2 py-1 text-gray-500">
                    {(a.at || "").replace("T", " ").slice(0, 19)}
                  </td>
                  <td className="px-2 py-1 text-gray-800">
                    <span className="mr-1 text-[10px] uppercase text-gray-400">
                      {a.plane === "azure" ? "AZ" : "ID"}
                    </span>
                    {a.operation}
                    {a.result && a.result.toLowerCase() !== "success" && (
                      <span className="ml-1 text-[10px] text-amber-700">({a.result})</span>
                    )}
                  </td>
                  <td className="max-w-[16rem] truncate px-2 py-1 text-gray-600"
                      title={a.target_type || a.target}>
                    {a.target}
                  </td>
                  <td className="px-2 py-1">
                    <Chip text={ATTRIBUTION[a.attribution]?.label ?? a.attribution}
                          cls={ATTRIBUTION[a.attribution]?.chip ?? "bg-gray-100 text-gray-600"}
                          title={ATTRIBUTION[a.attribution]?.hint} />
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
