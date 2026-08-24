/** Investigate — one identity, everything we know about it.
 *
 *  Sections are driven by `capabilities` from the server, NEVER by a switch on kind. A group
 *  has no sign-ins, a managed identity has no MFA, a guest has no licenses: deciding that
 *  here as well as on the server is how a component grows a bug per new principal kind.
 *
 *  Two rules the screen inherits from the API and must not soften:
 *    * unreadable is not empty — a section we could not read says so, and never renders as
 *      "nothing found", which is the opposite fact;
 *    * the Azure Activity Log is never fetched implicitly. It is per-subscription and slow,
 *      and this screen is linked from dozens of places.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type {
  InvestigateAction, InvestigateDossier, InvestigateHit, InvestigateKind,
  InvestigatePrincipal, InvestigateProvenance, InvestigateRecent, InvestigateSignin,
} from "../../api";
import { formatError } from "../../utils/format";
import { CoverageBanner, Segmented } from "./EntraShared";
import { InvestigateLink } from "./InvestigateLink";
import { MembersTree } from "./MembersTree";

const KIND_LABEL: Record<InvestigateKind, string> = {
  user: "User", guest: "Guest", group: "Group", servicePrincipal: "Service principal",
  managedIdentity: "Managed identity", platform: "Azure platform", unknown: "Unknown",
};

const KIND_GLYPH: Record<InvestigateKind, string> = {
  user: "👤", guest: "🌐", group: "👥", servicePrincipal: "🤖",
  managedIdentity: "⚙️", platform: "☁️", unknown: "❔",
};

/** The same data, ordered by what the reader came to do. Section ORDER only — no new data,
 *  which is why this is cheap enough to ship with the first version.
 *
 *  `members` is groups-only and simply absent for every other kind, so listing it here costs
 *  nothing when it does not apply. It leads `recertification` because that is the default
 *  lens for a group, and "who is in this thing" is the first question asked of one.
 *  `memberships` is its mirror — the groups the subject belongs to — and leads `offboarding`,
 *  where the groups someone must be removed from are the whole job. */
const LENSES = {
  overview: { label: "Overview", order: ["activity", "access", "memberships", "members", "findings", "timeline", "activations"] },
  offboarding: { label: "Offboarding", order: ["access", "memberships", "members", "activations", "timeline", "findings", "activity"] },
  recertification: { label: "Recertification", order: ["members", "memberships", "access", "activations", "findings", "timeline", "activity"] },
  workload: { label: "Workload identity", order: ["access", "memberships", "findings", "activity", "timeline", "activations"] },
  support: { label: "Support", order: ["access", "memberships", "members", "findings"] },
} as const;
type Lens = keyof typeof LENSES;

const WINDOWS = [
  { value: "1", label: "24h" }, { value: "3", label: "3 days" },
  { value: "7", label: "7 days" }, { value: "30", label: "30 days" },
];

/** Short names for the jump links — the section headings themselves stay in full English. */
const SECTION_LABEL: Record<string, string> = {
  activity: "Activity", access: "Access", findings: "Findings",
  timeline: "Changes", activations: "Activations", members: "Members",
  memberships: "Groups",
};

/**
 * The diff vocabulary from `app/iam/diff.py`. Rendering the raw class ("de_escalated") or —
 * worse — reading a field that does not exist and drawing a blank column, tells the reader
 * nothing. Widening access is colored; narrowing it is not.
 */
const CHANGE_CLASS: Record<string, { label: string; cls: string }> = {
  added: { label: "gained access", cls: "text-rose-700" },
  removed: { label: "lost access", cls: "text-emerald-700" },
  escalated: { label: "escalated", cls: "text-rose-700 font-medium" },
  de_escalated: { label: "de-escalated", cls: "text-emerald-700" },
  re_scoped: { label: "re-scoped", cls: "text-amber-700" },
  activated: { label: "activated", cls: "text-rose-700" },
  deactivated: { label: "deactivated", cls: "text-gray-600" },
  path_changed: { label: "path changed", cls: "text-amber-700" },
  orphaned: { label: "orphaned", cls: "text-amber-700" },
};

function defaultLens(kind: InvestigateKind): Lens {
  if (kind === "servicePrincipal" || kind === "managedIdentity") return "workload";
  if (kind === "group") return "recertification";
  return "overview";
}

// ---------------------------------------------------------------- small building blocks
function Prov({ p }: { p: InvestigateProvenance }) {
  return (
    <div className={`mt-1 text-[11px] ${p.unreadable ? "text-amber-700" : "text-gray-400"}`}>
      {p.unreadable ? "⚠ " : ""}
      {p.source}
      {p.collected_at ? ` · collected ${p.collected_at.slice(0, 16).replace("T", " ")}` : ""}
      {p.truncated ? " · truncated" : ""}
      {p.reason ? ` — ${p.reason}` : ""}
    </div>
  );
}

/** A section renders its own emptiness. "We could not read this" and "there is nothing
 *  here" are opposite facts and must not share a rendering.
 *
 *  `footer` renders in EVERY state, including unreadable and empty. That is not a
 *  convenience: an affordance that FETCHES the missing data must not be hidden by the data
 *  being missing, which is exactly what happens if it is passed as a child. */
function Section({
  id, title, count, prov, children, empty, footer,
}: {
  id: string; title: string; count?: number; prov?: InvestigateProvenance;
  children?: React.ReactNode; empty?: string; footer?: React.ReactNode;
}) {
  const unreadable = prov?.unreadable;
  return (
    <section data-testid={`investigate-section-${id}`}
             className="scroll-mt-14 rounded-xl border bg-white p-4">
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {count !== undefined && !unreadable && (
          <span className="rounded bg-gray-100 px-1.5 text-[11px] tabular-nums text-gray-600">{count}</span>
        )}
      </div>
      {unreadable ? (
        <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          This could not be read, so nothing is claimed about it. {prov?.reason}
        </div>
      ) : count === 0 ? (
        <div className="text-xs text-gray-500">{empty ?? "Nothing recorded."}</div>
      ) : (
        children
      )}
      {footer}
      {prov && <Prov p={prov} />}
    </section>
  );
}

function Table({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="max-h-72 overflow-auto rounded border">
      <table className="w-full text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-[11px] uppercase text-gray-500">
          <tr>{head.map((h, i) => <th key={i} className="px-2 py-1.5 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((r, i) => (
            <tr key={i} className="hover:bg-gray-50">
              {r.map((c, j) => <td key={j} className="px-2 py-1.5 align-top">{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ATTRIBUTION_STYLE: Record<string, { label: string; cls: string; title: string }> = {
  required_activation: {
    label: "needed elevation", cls: "bg-rose-50 text-rose-700 border-rose-200",
    title: "The principal held no standing role that covered this action.",
  },
  possible_without: {
    label: "standing role sufficed", cls: "bg-gray-100 text-gray-600 border-gray-200",
    title: "A standing role already allowed this — any elevation was incidental.",
  },
  unclassified: {
    label: "unclassified", cls: "bg-amber-50 text-amber-800 border-amber-200",
    title: "The standing picture was unreadable, so no claim is made either way.",
  },
};

/** Presentation only. The tier itself is graded server-side, by the same function the
 *  collector and the scoring use — a second list of role names here would be a second
 *  definition of "tenant takeover", and they would drift apart. */
const ROLE_TIER_META: Record<string, { cls: string; mark: string; title: string }> = {
  tier0: {
    cls: "border-rose-300 bg-rose-50 text-rose-800", mark: "⚠ ",
    title: "Tier 0 — a holder can take over the tenant.",
  },
  tier1: {
    cls: "border-amber-300 bg-amber-50 text-amber-900", mark: "",
    title: "Tier 1 — broad administrative power, or a documented path to tier 0.",
  },
  tier2: {
    cls: "border-sky-200 bg-sky-50 text-sky-800", mark: "",
    title: "Tier 2 — scoped or read-oriented administration.",
  },
};

function AttributionChip({ value }: { value: string }) {
  const s = ATTRIBUTION_STYLE[value];
  if (!s) return null;
  return (
    <span title={s.title} className={`rounded border px-1.5 py-0.5 text-[10px] ${s.cls}`}>{s.label}</span>
  );
}

// ---------------------------------------------------------------- recently investigated
/**
 * The strip that means you do not have to search for the same person twice.
 *
 * Read back from this user's own audit trail rather than a second store: every dossier view
 * is already recorded, so a separate history would be a duplicate — and an unaudited one.
 *
 * "Clear" writes a local watermark and asks the server for entries after it. It HIDES the
 * strip; it never deletes audit rows. A history you can erase is not an audit trail, and the
 * record of who investigated whom is the part that protects both parties.
 */
const RECENT_CLEARED_KEY = "azsup.investigate.clearedAt";

function useRecent(connectionId: string, limit: number) {
  const [clearedAt, setClearedAt] = useState<string>(
    () => localStorage.getItem(RECENT_CLEARED_KEY) || "");
  const q = useQuery({
    queryKey: ["entra", "investigate", "recent", connectionId, clearedAt, limit],
    queryFn: () => api.entraInvestigateRecent(connectionId || null,
                                              { limit, since: clearedAt || undefined }),
    staleTime: 15_000,
  });
  const clear = () => {
    const now = new Date().toISOString();
    localStorage.setItem(RECENT_CLEARED_KEY, now);
    setClearedAt(now);
  };
  return { entries: q.data?.recent ?? [], clear, refetch: q.refetch };
}

function RecentStrip({
  entries, currentId, onPick, onClear,
}: {
  entries: InvestigateRecent[];
  currentId?: string;
  onPick: (id: string) => void;
  onClear: () => void;
}) {
  if (!entries.length) return null;
  // Display names are not unique — a real tenant routinely has several objects sharing one
  // name. Three identical chips pointing at three different principals is a lie the tooltip
  // cannot undo, so collisions get a short id suffix.
  const nameCounts = entries.reduce<Record<string, number>>((acc, e) => {
    acc[e.display_name] = (acc[e.display_name] ?? 0) + 1;
    return acc;
  }, {});
  return (
    <div data-testid="investigate-recent" className="flex flex-wrap items-center gap-1 text-xs">
      <span className="mr-1 text-gray-400">Recently investigated</span>
      {entries.map((e) => {
        const current = e.id === currentId;
        const ambiguous = (nameCounts[e.display_name] ?? 0) > 1;
        return (
          <button
            key={e.id}
            onClick={() => onPick(e.id)}
            data-testid="investigate-recent-chip"
            aria-current={current ? "true" : undefined}
            title={`${KIND_LABEL[e.kind] ?? e.kind} · ${e.id}`}
            className={`inline-flex max-w-[210px] items-center gap-1 rounded-full border px-2 py-0.5 ${
              current
                ? "border-brand bg-brand/10 font-medium text-brand"
                : "bg-white text-gray-600 hover:bg-gray-50"
            }`}
          >
            <span>{KIND_GLYPH[e.kind] ?? "❔"}</span>
            <span className="truncate">{e.display_name}</span>
            {ambiguous && <span className="shrink-0 font-mono text-[10px] text-gray-400">{e.id.slice(0, 4)}</span>}
            {/* A principal that has since been deleted is still worth returning to — but it
                must not look like a live one. */}
            {e.resolution !== "resolved" && <span title={e.resolution} className="text-amber-600">⚠</span>}
          </button>
        );
      })}
      <button
        onClick={onClear}
        data-testid="investigate-recent-clear"
        title="Hide this list. The audit record of who was investigated is kept."
        className="ml-1 rounded px-1.5 py-0.5 text-[11px] text-gray-400 hover:bg-gray-100 hover:text-gray-600"
      >
        clear
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- the identity header
function Banners({ p }: { p: InvestigatePrincipal }) {
  const out: { tone: string; text: string }[] = [];
  if (p.resolution === "deleted") {
    out.push({ tone: "rose", text: "This object no longer exists in the directory. Its access assignments survived it — that is what you are looking at, and it is usually the finding." });
  }
  if (p.resolution === "cross_tenant") {
    out.push({ tone: "violet", text: `This principal lives in another organization's directory${p.managing_tenant?.name ? ` (${p.managing_tenant.name})` : ""} via Azure Lighthouse. Nothing about the principal itself is readable from this tenant.` });
  }
  if (p.resolution === "unreadable") {
    out.push({ tone: "amber", text: "The directory could not be read, so we cannot say what this principal is. This is not the same as it not existing." });
  }
  if (p.resolution === "not_found") {
    out.push({ tone: "gray", text: "No principal with that identifier exists in this tenant. Check the tenant selector — an object id means nothing without the directory it belongs to." });
  }
  if (p.sub_kind?.role_assignable) {
    out.push({ tone: "rose", text: "This group can hold Entra directory roles (isAssignableToRole). Membership of it is a privilege-escalation path." });
  }
  if (p.sub_kind?.dynamic) {
    out.push({ tone: "amber", text: "Membership of this group is a rule, not a list. Removing someone does not stick — the rule re-adds them." });
  }
  if (p.enabled === false) out.push({ tone: "amber", text: "⚠ This account is disabled." });
  const tones: Record<string, string> = {
    rose: "border-rose-200 bg-rose-50 text-rose-800",
    amber: "border-amber-200 bg-amber-50 text-amber-800",
    violet: "border-violet-200 bg-violet-50 text-violet-800",
    gray: "border-gray-200 bg-gray-50 text-gray-700",
  };
  if (!out.length) return null;
  return (
    <div className="space-y-1.5">
      {out.map((b, i) => (
        <div key={i} data-testid="investigate-banner" className={`rounded-lg border px-3 py-2 text-xs ${tones[b.tone]}`}>{b.text}</div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- the search empty state
function SearchPane({ connectionId, onPick }: { connectionId: string; onPick: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const recent = useRecent(connectionId, 25);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 250);
    return () => clearTimeout(t);
  }, [q]);
  const searchQ = useQuery({
    queryKey: ["entra", "investigate", "search", debounced, connectionId],
    queryFn: () => api.entraInvestigateSearch(debounced, connectionId || null),
    enabled: debounced.trim().length >= 2,
  });
  const hits: InvestigateHit[] = searchQ.data?.results ?? [];
  return (
    <div className="mx-auto max-w-2xl p-6">
      <h2 className="text-lg font-semibold text-gray-900">Investigate an identity</h2>
      <p className="mt-1 text-sm text-gray-500">
        Everything this product knows about one principal, in one place — who it is, what it can
        reach, how that changed, and what it did.
      </p>
      <input
        autoFocus
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Name, UPN, object id or app id…"
        aria-label="Search for an identity"
        data-testid="investigate-search"
        className="mt-4 w-full rounded-lg border px-3 py-2 text-sm"
      />
      {debounced.trim().length >= 2 && (
        <div className="mt-3 divide-y rounded-lg border bg-white">
          {searchQ.isLoading && <div className="p-3 text-xs text-gray-400">Searching…</div>}
          {!searchQ.isLoading && hits.length === 0 && (
            <div className="p-3 text-xs text-gray-500">
              Nothing in this tenant's directory matches. If you have an object id from an access
              assignment, paste it — a deleted or cross-tenant principal still resolves.
            </div>
          )}
          {hits.map((h) => (
            <button
              key={h.id}
              onClick={() => onPick(h.id)}
              data-testid="investigate-hit"
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-50"
            >
              <span>{KIND_GLYPH[h.kind]}</span>
              <span className="min-w-0 flex-1 truncate">{h.display_name}</span>
              <span className="truncate text-xs text-gray-400">{h.upn || h.app_id}</span>
              <span className="rounded bg-gray-100 px-1.5 text-[10px] text-gray-600">{KIND_LABEL[h.kind]}</span>
            </button>
          ))}
        </div>
      )}
      <button
        onClick={() => onPick(q.trim())}
        disabled={q.trim().length < 4}
        className="mt-3 rounded border px-2 py-1 text-xs text-gray-600 disabled:opacity-40"
      >
        Investigate this identifier exactly
      </button>

      {/* The most valuable place for the history: this is the screen you land on when you do
          not have a link, and it is where searching the same person twice actually happens. */}
      {recent.entries.length > 0 && (
        <div className="mt-6 border-t pt-4">
          <RecentStrip entries={recent.entries} onPick={onPick} onClear={recent.clear} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- activity
function ActivityPanel({
  principalId, capabilities, connectionId,
}: { principalId: string; capabilities: string[]; connectionId: string }) {
  const [days, setDays] = useState("1");
  const [justification, setJustification] = useState("");
  const [includeAzure, setIncludeAzure] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.entraInvestigateActivity>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [denied, setDenied] = useState(false);
  const [cancelled, setCancelled] = useState(false);

  const cheap = ["signins", "signins_noninteractive", "audit", "risk"].filter((t) => capabilities.includes(t));
  const canAzure = capabilities.includes("azure_activity");

  const abortRef = useRef<AbortController | null>(null);

  async function run(withAzure: boolean) {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true); setErr(""); setCancelled(false);
    try {
      const types = [...cheap, ...(withAzure && canAzure ? ["azure_activity"] : [])];
      setResult(await api.entraInvestigateActivity(
        principalId, { types, days: Number(days), justification }, connectionId || null,
        ctrl.signal));
    } catch (e) {
      if ((e as Error)?.name === "AbortError") { setCancelled(true); return; }
      const msg = formatError(e);
      setErr(msg);
      if (/403|permission/i.test(msg)) setDenied(true);
    } finally {
      // Only the newest read owns the busy flag; an aborted one must not clear its successor's.
      if (abortRef.current === ctrl) { abortRef.current = null; setBusy(false); }
    }
  }

  // Read on arrival, ONCE per principal. Keyed on the id rather than on mount so that changing
  // the window or re-rendering does not re-fire it: this POST is an audited act against a named
  // person, and it must not repeat itself just because the component drew again. The Azure
  // Activity Log stays excluded — it is per-subscription and slow, and never implicit.
  const autoReadFor = useRef("");
  useEffect(() => {
    if (!principalId || !cheap.length || denied) return;
    if (autoReadFor.current === principalId) return;
    autoReadFor.current = principalId;
    void run(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [principalId, cheap.length, denied]);

  if (!cheap.length && !canAzure) return null;

  return (
    <section data-testid="investigate-section-activity"
             className="scroll-mt-14 rounded-xl border bg-white p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-gray-900">Activity</h3>
        <Segmented value={days} onChange={setDays} options={WINDOWS} label="Window" />
        <input
          value={justification}
          onChange={(e) => setJustification(e.target.value)}
          placeholder="Ticket or reason (recorded)"
          aria-label="Justification"
          data-testid="investigate-justification"
          className="w-56 rounded border px-2 py-1 text-xs"
        />
        <button
          onClick={() => void run(includeAzure)}
          disabled={busy}
          aria-busy={busy}
          data-testid="investigate-run-activity"
          // Dimmed less than a normally-disabled control: this one is working, not unavailable.
          className="inline-flex items-center gap-1.5 rounded bg-brand px-2.5 py-1 text-xs font-medium text-white disabled:opacity-70"
        >
          {busy && (
            <span
              aria-hidden="true"
              className="h-3 w-3 animate-spin rounded-full border-2 border-white/40 border-t-white motion-reduce:animate-none"
            />
          )}
          {busy ? "Reading…" : "Read activity"}
        </button>
        {busy && (
          <button
            onClick={() => abortRef.current?.abort()}
            data-testid="investigate-cancel-activity"
            title="Stops waiting for the result. The request already reached the server, so the read is recorded and may still finish there."
            className="rounded border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
          >
            Cancel
          </button>
        )}
        <label className="flex items-center gap-1 text-[11px] text-gray-600" title="Per-subscription and slow — never read unless you ask.">
          <input type="checkbox" checked={includeAzure} disabled={!canAzure}
                 onChange={(e) => setIncludeAzure(e.target.checked)} />
          include Azure Activity Log (slow)
        </label>
      </div>
      <p className="mb-2 text-[11px] text-gray-500">
        Reading a named identity's sign-in and audit history is behavioral data. Who asked, about
        whom, and why is recorded in the audit log.
      </p>

      {cancelled && !busy && (
        <div className="mb-2 rounded border border-gray-200 bg-gray-50 p-2 text-[11px] text-gray-600">
          Read cancelled. Only the wait was stopped — the request had already reached the server,
          so it is recorded in the audit log either way.
        </div>
      )}

      {denied && (
        <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          Your role can see this identity's access and findings but not its behavioral history.
          That split is deliberate — <code>investigate.activity</code> is granted separately.
        </div>
      )}
      {err && !denied && <div className="rounded border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">{err}</div>}

      {result && (
        <div className="space-y-3">
          {result.notes?.length > 0 && (
            <ul className="list-disc space-y-0.5 rounded bg-gray-50 p-2 pl-6 text-[11px] text-gray-600">
              {result.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}
          {result.attribution?.counts && (
            <div className="flex flex-wrap gap-2 text-[11px]">
              {Object.entries(ATTRIBUTION_STYLE).map(([k, s]) => (
                <span key={k} title={s.title} className={`rounded border px-2 py-0.5 ${s.cls}`}>
                  {s.label}: <b className="tabular-nums">{result.attribution.counts?.[k] ?? 0}</b>
                </span>
              ))}
            </div>
          )}
          {(() => {
            // Portal vocabulary: a workload identity's sign-ins are all it has, so the
            // interactive/non-interactive split is a user distinction only.
            const kind = result.principal?.kind;
            const interactiveTitle =
              kind === "servicePrincipal" ? "Service principal sign-ins"
              : kind === "managedIdentity" ? "Managed identity sign-ins"
              : "User sign-ins (interactive)";
            return ([
              ["signins", interactiveTitle, "No sign-in recorded in this window."],
              ["signins_noninteractive", "User sign-ins (non-interactive)",
               "No non-interactive sign-in recorded in this window."],
            ] as const).map(([key, title, empty]) => {
              const sec = result.sections[key];
              if (!sec) return null;
              return (
                <Section key={key} id={key} title={title} count={sec.data.length}
                         prov={sec.provenance} empty={empty}>
                  <Table
                    head={["When", "App", "Result", "From", "CA"]}
                    rows={sec.data.map((s: InvestigateSignin) => [
                      s.at.slice(0, 19).replace("T", " "),
                      s.app,
                      s.success ? <span className="text-emerald-700">success</span>
                                : <span className="text-rose-700" title={s.failure_reason}>{s.failure_code}</span>,
                      [s.city, s.country].filter(Boolean).join(", ") || s.ip,
                      s.ca_status,
                    ])}
                  />
                </Section>
              );
            });
          })()}
          {result.sections.risk && (
            <Section id="risk" title="Risk detections" count={result.sections.risk.data.length}
                     prov={result.sections.risk.provenance} empty="Identity Protection raised nothing in this window.">
              <Table head={["When", "Type", "Level", "State"]}
                     rows={result.sections.risk.data.map((r) => [
                       r.at.slice(0, 19).replace("T", " "), r.type, r.level, r.state])} />
            </Section>
          )}
          {(["audit", "azure_activity"] as const).map((key) => {
            const sec = result.sections[key];
            if (!sec) return null;
            return (
              <Section key={key} id={key}
                       title={key === "audit" ? "Directory changes they made" : "Azure changes they made"}
                       count={sec.data.length} prov={sec.provenance}
                       empty="No change attributed to this principal in this window.">
                <Table
                  head={["When", "Operation", "Target", "Result", "Needed elevation?"]}
                  rows={sec.data.map((a: InvestigateAction) => [
                    a.at.slice(0, 19).replace("T", " "), a.operation, a.target, a.result,
                    <AttributionChip value={a.attribution} />,
                  ])}
                />
              </Section>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- the screen
export function EntraInvestigateView({ connectionId }: { connectionId: string }) {
  const lensBarRef = useRef<HTMLDivElement>(null);
  const [principalId, setPrincipalId] = useState(
    () => new URLSearchParams(window.location.search).get("principal_id") || "");

  // Deep links land here with ?principal_id=. Reflect the subject in the URL so an
  // investigation can be shared, and so Back returns to the search rather than nowhere.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (principalId) url.searchParams.set("principal_id", principalId);
    else url.searchParams.delete("principal_id");
    if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url);
  }, [principalId]);

  useEffect(() => {
    const onPop = () =>
      setPrincipalId(new URLSearchParams(window.location.search).get("principal_id") || "");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const q = useQuery({
    queryKey: ["entra", "investigate", principalId, connectionId],
    queryFn: () => api.entraInvestigate(principalId, connectionId || null),
    enabled: !!principalId,
  });

  const dossier = q.data as InvestigateDossier | undefined;
  const principal = dossier?.principal;
  const caps = dossier?.capabilities ?? [];
  const [lens, setLens] = useState<Lens | "">("");
  const [notesOpen, setNotesOpen] = useState(false);
  const activeLens: Lens = (lens || (principal ? defaultLens(principal.kind) : "overview")) as Lens;

  const sectionOrder = useMemo(() => LENSES[activeLens].order, [activeLens]);

  // A lens lists every section it could show, and several are absent for most kinds — a group
  // has no sign-ins, a user has no members. Both the jump links and the scroll target have to
  // read from the same rule, or one of them points at a section that is not on the page.
  const isRendered = useCallback((name: string) => {
    if (!dossier) return false;
    if (name === "activity") {
      return ["signins", "signins_noninteractive", "audit", "risk", "azure_activity"]
        .some((t) => caps.includes(t));
    }
    return !!dossier.sections[name as keyof typeof dossier.sections];
  }, [dossier, caps]);

  const jumpTargets = useMemo(() => sectionOrder.filter(isRendered), [sectionOrder, isRendered]);

  const recent = useRecent(connectionId, 8);
  // The dossier request is what WRITES the audit row this strip reads, so the strip has to be
  // re-read after it lands — otherwise the person you are looking at never joins the list.
  useEffect(() => {
    if (q.isSuccess) void recent.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.isSuccess, principalId]);

  /**
   * A lens reorders five stacked sections in a page three times the height of the viewport.
   * Without this the reader switches lens, sees no change (the control is at the top, the
   * section it promoted is 2,000px down), and concludes the button is broken. Switching a
   * lens must land you on what that lens leads with.
   *
   * Scrolling the lens bar itself does NOT do that: the bar is `sticky top-0`, so once the
   * reader is anywhere below the fold it is already at the top of the viewport and
   * `scrollIntoView` is a no-op — they stay exactly where they were and the promoted section
   * is still off screen above them. The target has to be the leading SECTION, which carries
   * `scroll-mt-14` to clear the bar.
   */
  const pickLens = (v: string) => {
    setLens(v as Lens);
    const lead = LENSES[v as Lens].order.find(isRendered);
    // Two frames: the first lets React commit the reorder, the second measures it.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const target = lead
        ? document.querySelector(`[data-testid="investigate-section-${lead}"]`)
        : lensBarRef.current;
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  };

  if (!principalId) {
    return <SearchPane connectionId={connectionId} onPick={setPrincipalId} />;
  }

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button onClick={() => setPrincipalId("")} className="text-xs text-brand hover:underline">
          ← Investigate someone else
        </button>
        <RecentStrip
          entries={recent.entries}
          currentId={principal?.id}
          onPick={setPrincipalId}
          onClear={recent.clear}
        />
      </div>

      {q.isLoading && <div className="p-6 text-sm text-gray-400">Loading…</div>}
      {q.error && <div className="rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{formatError(q.error)}</div>}

      {dossier && principal && (
        <>
          <CoverageBanner meta={dossier.meta} />

          <header data-testid="investigate-header" className="rounded-xl border bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xl">{KIND_GLYPH[principal.kind]}</span>
              <h2 className="text-base font-semibold text-gray-900">{principal.display_name}</h2>
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
                {KIND_LABEL[principal.kind]}
              </span>
              {principal.enabled === false && (
                <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                  ⚠ disabled
                </span>
              )}
              <div className="ml-auto flex gap-2">
                <a
                  href={api.entraInvestigateExportUrl(principal.id, connectionId || null)}
                  data-testid="investigate-export"
                  className="rounded border px-2 py-1 text-xs text-gray-700 hover:bg-gray-50"
                >
                  ⬇ Export
                </a>
              </div>
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-600 sm:grid-cols-4">
              {principal.upn && <div><dt className="text-gray-400">UPN</dt><dd className="truncate">{principal.upn}</dd></div>}
              {principal.app_id && <div><dt className="text-gray-400">App id</dt><dd className="truncate font-mono">{principal.app_id}</dd></div>}
              <div><dt className="text-gray-400">Object id</dt><dd className="truncate font-mono">{principal.id}</dd></div>
              <div><dt className="text-gray-400">Tenant</dt><dd className="truncate font-mono">{dossier.meta.tenant_id}</dd></div>
              {String(principal.sub_kind?.assigned_to_resource || "") && (
                <div className="col-span-2 sm:col-span-4">
                  <dt className="text-gray-400">Owned by resource</dt>
                  <dd className="truncate font-mono">{String(principal.sub_kind.assigned_to_resource)}</dd>
                </div>
              )}
            </dl>
          </header>

          <Banners p={principal} />

          {dossier.notes.length > 0 && (
            <div className="rounded-lg border bg-gray-50 text-[11px] text-gray-600">
              <button
                onClick={() => setNotesOpen((v) => !v)}
                aria-expanded={notesOpen}
                data-testid="investigate-notes-toggle"
                className="flex w-full items-center gap-1 px-3 py-1.5 text-left hover:bg-gray-100"
              >
                <span className="text-gray-400">{notesOpen ? "\u25BE" : "\u25B8"}</span>
                {/* One line by default: these explain why a section is ABSENT, which matters
                    once and then costs a fifth of the viewport on every scroll past. */}
                <span>
                  {dossier.notes.length} note{dossier.notes.length > 1 ? "s" : ""} on what this
                  kind of identity does not have
                </span>
              </button>
              {notesOpen && (
                <ul className="list-disc space-y-0.5 border-t px-3 py-2 pl-7">
                  {dossier.notes.map((n, i) => <li key={i}>{n}</li>)}
                </ul>
              )}
            </div>
          )}

          {/* Sticky, because the sections below run to three viewport-heights. A control that
              scrolls away strands the reader: they scroll down to read, then cannot get back to
              switch lens without scrolling all the way up. The jump links are the same fix for
              the sections themselves. */}
          <div
            ref={lensBarRef}
            className="sticky top-0 z-10 -mx-4 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b bg-gray-50/95 px-4 py-2 backdrop-blur"
          >
            <Segmented
              value={activeLens}
              onChange={pickLens}
              options={(Object.keys(LENSES) as Lens[]).map((id) => ({ value: id, label: LENSES[id].label }))}
              label="Lens"
            />
            <nav aria-label="Jump to section" className="flex flex-wrap items-center gap-1">
              {jumpTargets.map((name) => (
                <button
                  key={name}
                  data-testid={`investigate-jump-${name}`}
                  onClick={() => document
                    .querySelector(`[data-testid="investigate-section-${name}"]`)
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })}
                  className="rounded px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-white hover:text-brand"
                >
                  {SECTION_LABEL[name] ?? name}
                </button>
              ))}
            </nav>
          </div>

          {sectionOrder.map((name) => {
            if (name === "activity") {
              return (
                <ActivityPanel key="activity" principalId={principal.id} capabilities={caps}
                               connectionId={connectionId} />
              );
            }
            if (name === "access") {
              const s = dossier.sections.access;
              const d = s.data;
              const total = d.directory_roles.length + d.azure_assignment_count;
              return (
                <Section key="access" id="access" title="Access" count={total} prov={s.provenance}
                         empty="This principal holds no directory role and no Azure assignment we can see.">
                  <div className="space-y-3">
                    {d.directory_roles.length > 0 && (
                      <div>
                        <div className="mb-1 text-[11px] uppercase text-gray-500">Entra directory roles</div>
                        <div className="flex flex-wrap gap-1">
                          {d.directory_roles.map((r) => {
                            // A PIM-eligible role is not held. Rendering it in the same chip
                            // as a standing one told the reader this account permanently has
                            // Global Administrator when in fact it has to activate for it.
                            const eligibleOnly = (d.directory_roles_eligible_only ?? []).includes(r);
                            const meta = ROLE_TIER_META[(d.directory_role_tiers ?? {})[r] ?? "tier2"]
                              ?? ROLE_TIER_META.tier2;
                            return (
                              <span
                                key={r}
                                title={`${meta.title} ${eligibleOnly
                                  ? "PIM-eligible: must be activated, not held right now."
                                  : "Held now."}`}
                                className={`rounded border px-1.5 py-0.5 text-xs ${meta.cls} ${
                                  eligibleOnly ? "border-dashed" : ""}`}
                              >
                                {meta.mark}{r}{eligibleOnly ? " · eligible" : ""}
                              </span>
                            );
                          })}
                        </div>
                        {/* Colour alone cannot carry a risk claim — the tiers are named. */}
                        <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-gray-500">
                          <span><span className="text-rose-700">■</span> tier 0 · tenant takeover</span>
                          <span><span className="text-amber-700">■</span> tier 1 · broad admin</span>
                          <span><span className="text-sky-700">■</span> tier 2 · scoped</span>
                          <span>dashed · eligible, not held</span>
                        </div>
                      </div>
                    )}
                    {d.azure_assignment_count > 0 && (
                      <div>
                        <div className="mb-1 text-[11px] uppercase text-gray-500">
                          Azure RBAC · {d.azure_assignment_count} assignment(s)
                        </div>
                        <Table
                          head={["Role", "Scope", "Path", "Eligible"]}
                          rows={d.azure_assignments.slice(0, 200).map((r) => {
                            // "group" alone is a dead end: it says the access is inherited
                            // and refuses to say from what. The composer already carries the
                            // name — not showing it sent the reader to another tool.
                            const via = String(r.sourceGroupName || r.membershipGroupName || "");
                            const path = String(r.accessPath ?? "");
                            const priv = Boolean(r.roleIsPrivileged);
                            return [
                              priv
                                ? <span className="font-medium text-rose-700"
                                        title="Privileged — grants write, role-assignment or credential access.">
                                    ⚠ {String(r.roleName ?? "")}
                                  </span>
                                : String(r.roleName ?? ""),
                              String(r.subscriptionName || r.scope || ""),
                              via ? `${path} · ${via}` : path,
                              r.eligible ? "eligible" : "standing",
                            ];
                          })}
                        />
                      </div>
                    )}
                  </div>
                </Section>
              );
            }
            if (name === "members") {
              const s = dossier.sections.members;
              // Absent for every kind except a group. The lens order lists it unconditionally
              // so the orders stay readable; this is where it costs nothing when it is absent.
              if (!s) return null;
              const d = s.data;
              return (
                <Section key="members" id="members" title="Members" count={d.count}
                         prov={s.provenance}
                         empty="No member reached Azure access through this group."
                         /* The tree is the REMEDY for this list being absent — only groups
                            holding an Azure assignment are expanded, so most groups have no
                            cached membership at all. Hiding the live fetch behind the cache
                            having data would hide it precisely when it is the only answer. */
                         footer={
                           <MembersTree principalId={principal.id}
                                        rootName={principal.display_name || principal.id}
                                        connectionId={connectionId} />
                         }>
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-1">
                      {d.dynamic && (
                        <span title={d.membership_rule || "Membership is evaluated from a rule."}
                              className="rounded border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] text-sky-800">
                          dynamic — membership is a rule's output
                        </span>
                      )}
                      {d.on_prem_synced && (
                        <span title="Members are added and removed in on-premises AD, not in Entra. Changes here will be overwritten by the next sync."
                              className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800">
                          synced from on-premises AD
                        </span>
                      )}
                      {d.role_assignable && (
                        <span title="This group can be assigned an Entra directory role, so its membership is a privileged-access control."
                              className="rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] text-rose-800">
                          role-assignable
                        </span>
                      )}
                    </div>
                    {d.membership_rule && (
                      <pre className="overflow-auto rounded bg-gray-50 p-2 text-[10px] text-gray-700">{d.membership_rule}</pre>
                    )}
                    {d.count > 0 && (
                      <Table
                        head={["Member", "Kind", "UPN", ""]}
                        rows={d.members.slice(0, 200).map((m) => [
                          m.display_name || m.id,
                          m.kind,
                          m.upn,
                          <InvestigateLink principalId={m.id} label={m.display_name || m.id} />,
                        ])}
                      />
                    )}
                  </div>
                </Section>
              );
            }
            if (name === "memberships") {
              const s = dossier.sections.memberships;
              // Absent for the Azure platform and for anything we could not resolve.
              if (!s) return null;
              const d = s.data;
              return (
                <Section key="memberships" id="memberships" title="Groups it belongs to"
                         count={d.count} prov={s.provenance}
                         empty="No group that grants Azure RBAC, an Entra directory role or a Conditional Access target contains this principal. Other memberships are not collected — read the directory live to see them."
                         /* Same reasoning as `members`: the live read is the REMEDY for the
                            cache only ever holding groups that grant something, so it must
                            render when the list is empty or unreadable, not only when the
                            cache already answered. */
                         footer={
                           <MembersTree principalId={principal.id}
                                        rootName={principal.display_name || principal.id}
                                        rootKind={principal.kind}
                                        connectionId={connectionId}
                                        mode="up" />
                         }>
                  <div className="space-y-2">
                    {d.role_assignable_count > 0 && (
                      <div className="rounded border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] text-rose-800">
                        {d.role_assignable_count} of these can be assigned an Entra directory
                        role. Membership of one is a privilege-escalation path in its own right.
                      </div>
                    )}
                    <Table
                      head={["Group", "Why it matters", "Caveats", ""]}
                      rows={d.groups.slice(0, 200).map((g) => [
                        g.display_name || g.id,
                        <span className="flex flex-wrap gap-1">
                          {g.sources.map((src) => (
                            <span key={src} className="rounded border bg-gray-50 px-1 py-0.5 text-[10px] text-gray-700">
                              {d.source_labels[src] ?? src}
                            </span>
                          ))}
                        </span>,
                        <span className="flex flex-wrap gap-1">
                          {g.role_assignable && (
                            <span title="This group can be assigned an Entra directory role."
                                  className="rounded border border-rose-200 bg-rose-50 px-1 py-0.5 text-[10px] text-rose-800">
                              role-assignable
                            </span>
                          )}
                          {g.dynamic && (
                            <span title={g.membership_rule || "Membership is evaluated from a rule — removing this principal does not stick."}
                                  className="rounded border border-sky-200 bg-sky-50 px-1 py-0.5 text-[10px] text-sky-800">
                              dynamic
                            </span>
                          )}
                          {g.on_prem_synced && (
                            <span title="Membership is authored in on-premises AD. A change made in Entra is overwritten by the next sync."
                                  className="rounded border border-amber-200 bg-amber-50 px-1 py-0.5 text-[10px] text-amber-800">
                              on-prem
                            </span>
                          )}
                        </span>,
                        <InvestigateLink principalId={g.id} label={g.display_name || g.id} />,
                      ])}
                    />
                  </div>
                </Section>
              );
            }
            if (name === "findings") {
              const s = dossier.sections.findings;
              return (
                <Section key="findings" id="findings" title="Findings" count={s.data.length}
                         prov={s.provenance} empty="No signal has fired against this principal.">
                  <Table head={["Severity", "Finding"]}
                         rows={s.data.map((f) => [String(f.severity ?? ""), String(f.title ?? f.signal_id ?? "")])} />
                </Section>
              );
            }
            if (name === "timeline") {
              const s = dossier.sections.timeline;
              return (
                <Section key="timeline" id="timeline" title="How their access changed"
                         count={s.data.events.length} prov={s.provenance}
                         empty="No access change was captured for this principal.">
                  <Table head={["When", "Change", "Role", "Scope"]}
                         rows={s.data.events.map((e) => [
                           String(e.at ?? "").slice(0, 19).replace("T", " "),
                           <span className={CHANGE_CLASS[String(e.class ?? "")]?.cls ?? "text-gray-600"}>
                             {CHANGE_CLASS[String(e.class ?? "")]?.label ?? String(e.class ?? "changed")}
                           </span>,
                           String(e.roleName ?? ""),
                           String(e.scopeName || e.scope || ""),
                         ])} />
                </Section>
              );
            }
            const s = dossier.sections.activations;
            return (
              <Section key="activations" id="activations" title="Privilege activations"
                       count={s.data.length} prov={s.provenance}
                       empty="This principal has never activated a privileged role.">
                <Table head={["Start", "Role", "Scope", "Justification"]}
                       rows={s.data.map((a) => [
                         String(a.start ?? "").slice(0, 19).replace("T", " "),
                         String(a.role_name ?? ""), String(a.scope_name ?? ""),
                         String(a.justification ?? ""),
                       ])} />
              </Section>
            );
          })}
        </>
      )}
    </div>
  );
}
