import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import type { EntraInboxRow, EntraScanner, EntraScannerRun } from "../../api";
import { formatError } from "../../utils/format";
import { IdentityFindingsPanel } from "../IdentityView";
import { CoverageBanner, EntraEmpty, SevBadge, SortScopeNote, SortTh, useSortState, useSubTabRoute } from "./EntraShared";
import { FindingDrawer } from "./EntraFindingsView";

// "hygiene" is the former /identity overview. It is kept separate from the inbox because it
// comes from a different pipeline (the ARM-backed identity cache with its own refresh) rather
// than the Entra snapshot — folding its rows into the inbox would silently mix two freshness
// models under one "last refreshed" claim.
type Tab = "inbox" | "scanners" | "hygiene";

const TABS: [Tab, string][] = [
  ["inbox", "Findings inbox"],
  ["scanners", "Scanners"],
  ["hygiene", "Identity hygiene"],
];

const STATE_LABEL: Record<string, string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  snoozed: "Snoozed",
  suppressed: "Suppressed",
};

/**
 * Inbox sort columns, matched to the keys `GET /entra/inbox` accepts.
 *
 * `""` is "the server's own ordering" (severity, then age, then object), which is what an
 * operator sees before they express any preference of their own.
 */
type InboxSortKey = "" | "severity" | "title" | "object" | "state" | "age";

function Chip({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-2.5 py-0.5 text-[12px] ${
        active ? "border-gray-800 bg-gray-800 text-white" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"
      }`}
    >
      {children}
    </button>
  );
}

function InboxTab({ connectionId }: { connectionId: string | null }) {
  const qc = useQueryClient();
  const [severity, setSeverity] = useState("");
  const [state, setState] = useState("");
  const [ageing, setAgeing] = useState<number | undefined>(undefined);
  const [unassigned, setUnassigned] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [action, setAction] = useState("acknowledged");
  const [reason, setReason] = useState("");
  const [snoozeDays, setSnoozeDays] = useState(14);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<EntraInboxRow | null>(null);
  // Server-side, for the same reason as the findings list: the request is capped at 500
  // rows, and sorting a cap in the browser answers a different question than it claims to.
  const [sort, setSort] = useSortState<InboxSortKey>("inbox", { key: "", dir: -1 });

  const q = useQuery({
    queryKey: ["entra-inbox", connectionId, severity, state, ageing, unassigned, search, sort.key, sort.dir],
    queryFn: () => {
      const params = {
        severity: severity || undefined, state: state || undefined,
        ageing_days: ageing, unassigned: unassigned || undefined,
        search: search || undefined, limit: 500,
        // No key means no parameter, so the server's default ordering stands untouched.
        ...(sort.key ? { sort: sort.key, dir: sort.dir === 1 ? "asc" : "desc" } : {}),
      };
      return api.entraInbox(params, connectionId);
    },
  });

  const bulk = useMutation({
    mutationFn: () => api.entraInboxBulk({
      fingerprints: [...selected], state: action, reason,
      snooze_days: action === "snoozed" ? snoozeDays : 0,
    }, connectionId),
    onSuccess: () => {
      setSelected(new Set());
      setReason("");
      setError("");
      qc.invalidateQueries({ queryKey: ["entra-inbox"] });
      qc.invalidateQueries({ queryKey: ["entra-posture"] });
    },
    onError: (e) => setError(formatError(e)),
  });

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading the inbox…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  const toggle = (fp: string) => {
    const next = new Set(selected);
    if (next.has(fp)) next.delete(fp); else next.add(fp);
    setSelected(next);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-2 border-b bg-white px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip active={severity === "critical"} onClick={() => setSeverity(severity === "critical" ? "" : "critical")}>
            Critical {d.by_severity.critical ?? 0}
          </Chip>
          <Chip active={severity === "high"} onClick={() => setSeverity(severity === "high" ? "" : "high")}>
            High {d.by_severity.high ?? 0}
          </Chip>
          <Chip active={state === "open"} onClick={() => setState(state === "open" ? "" : "open")}>
            Open {d.by_state.open ?? 0}
          </Chip>
          <Chip active={state === "snoozed"} onClick={() => setState(state === "snoozed" ? "" : "snoozed")}>
            Snoozed {d.by_state.snoozed ?? 0}
          </Chip>
          <Chip active={unassigned} onClick={() => setUnassigned(!unassigned)}>Unassigned</Chip>
          <Chip active={ageing === 30} onClick={() => setAgeing(ageing === 30 ? undefined : 30)}>
            Ageing &gt; 30d
          </Chip>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search…"
            className="ml-auto w-48 rounded border px-2 py-1 text-[13px]"
          />
        </div>
        <div className="text-[11px] text-gray-500">
          {d.total} finding(s) · {d.suppressed_count} suppressed ·{" "}
          {d.recently_resolved.length} resolved automatically because the condition stopped appearing
        </div>
      </div>

      {selected.size > 0 && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b bg-amber-50 px-3 py-2">
          <span className="text-[13px] font-medium text-amber-900">{selected.size} selected</span>
          <select value={action} onChange={(e) => setAction(e.target.value)}
                  className="rounded border px-2 py-1 text-[13px]">
            {Object.entries(STATE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          {action === "snoozed" && (
            <input type="number" min={1} value={snoozeDays}
                   onChange={(e) => setSnoozeDays(Number(e.target.value))}
                   className="w-20 rounded border px-2 py-1 text-[13px]" title="Days" />
          )}
          {(action === "suppressed" || action === "snoozed") && (
            <input value={reason} onChange={(e) => setReason(e.target.value)}
                   placeholder={action === "suppressed" ? "Reason (required)" : "Reason"}
                   className="w-72 rounded border px-2 py-1 text-[13px]" />
          )}
          <button
            onClick={() => bulk.mutate()}
            disabled={bulk.isPending || (action === "suppressed" && !reason.trim())}
            className="rounded bg-gray-800 px-3 py-1 text-[13px] text-white disabled:opacity-50"
          >
            {bulk.isPending ? "Applying…" : "Apply"}
          </button>
          <button onClick={() => setSelected(new Set())}
                  className="text-[13px] text-gray-600 hover:underline">Clear</button>
          {error && <span className="text-[12px] text-red-600">{error}</span>}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full text-[13px]">
          <thead className="sticky top-0 bg-white text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr className="border-b">
              {/* Selection column: nothing to order by, and a sortable blank header would
                  only invite a click that does nothing. */}
              <th className="w-8 px-2 py-1.5"></th>
              <SortTh label="Severity" col="severity" sort={sort} setSort={setSort} />
              <SortTh label="Finding" col="title" sort={sort} setSort={setSort} firstDir={1} />
              <SortTh label="Object" col="object" sort={sort} setSort={setSort} firstDir={1} />
              <SortTh label="State" col="state" sort={sort} setSort={setSort} />
              <SortTh label="Age" col="age" sort={sort} setSort={setSort} className="pr-3" />
            </tr>
          </thead>
          <tbody>
            {d.findings.map((f: EntraInboxRow) => (
              <tr key={f.fingerprint} className="cursor-pointer border-b last:border-b-0 hover:bg-gray-50"
                  onClick={() => setOpen(f)}>
                <td className="px-2 py-1.5" onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={selected.has(f.fingerprint)}
                         onChange={() => toggle(f.fingerprint)} />
                </td>
                <td><SevBadge sev={f.severity} /></td>
                <td className="pr-2">
                  <div className="text-gray-900">{f.title}</div>
                  <div className="text-[11px] text-gray-500">{f.signal_id}</div>
                </td>
                <td className="pr-2 text-gray-700">{f.object_name}</td>
                <td>
                  <span className={`text-[12px] ${
                    f.state === "open" ? "text-gray-700" :
                    f.state === "snoozed" ? "text-amber-700" :
                    f.state === "suppressed" ? "text-gray-400" : "text-sky-700"}`}>
                    {STATE_LABEL[f.state] || f.state}
                  </span>
                  {f.assignee && <div className="text-[11px] text-gray-500">{f.assignee}</div>}
                </td>
                <td className="pr-3">
                  <span className={(f.age_days ?? 0) > 60 ? "font-medium text-amber-700" : "text-gray-600"}>
                    {f.age_days === null ? "—" : `${f.age_days}d`}
                  </span>
                </td>
              </tr>
            ))}
            {!d.findings.length && (
              <tr><td colSpan={6} className="py-6 text-center text-sm text-gray-500">
                Nothing matches this filter.
              </td></tr>
            )}
          </tbody>
        </table>
        <SortScopeNote shown={d.findings.length} total={d.total} />
      </div>

      {open && (
        <FindingDrawer
          finding={open}
          connectionId={connectionId}
          onClose={() => setOpen(null)}
          onChanged={() => {
            qc.invalidateQueries({ queryKey: ["entra-inbox"] });
            qc.invalidateQueries({ queryKey: ["entra-posture"] });
          }}
        />
      )}
    </div>
  );
}

/**
 * The findings a scanner reports right now.
 *
 * Fetched read-only rather than taken from the run response, so results survive a reload and
 * can be reviewed without running the scanner again (running would consume the new/resolved
 * delta and make the next run report "nothing changed").
 */
function ScannerFindings({ scannerId, connectionId, floor }: {
  scannerId: string; connectionId: string | null; floor: string;
}) {
  const q = useQuery({
    queryKey: ["entra-scanner-findings", connectionId, scannerId],
    queryFn: () => api.entraScannerFindings(scannerId, connectionId),
  });

  if (q.isLoading) return <div className="mt-2 text-[12px] text-gray-500">Loading findings…</div>;
  if (q.isError) return <div className="mt-2 text-[12px] text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (d.blocked) return <div className="mt-2 text-[12px] text-amber-800">{d.blocked}</div>;
  if (!d.total) {
    return (
      <div className="mt-2 rounded border border-green-200 bg-green-50 px-2 py-1.5 text-[12px] text-green-800">
        Nothing to report — this scanner looked at the current snapshot and found no findings
        at or above <span className="font-medium">{floor}</span> severity.
      </div>
    );
  }

  return (
    <div className="mt-2 overflow-hidden rounded border border-gray-200">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b bg-gray-50 px-2 py-1 text-[11px] text-gray-600">
        <span className="font-medium text-gray-800">{d.total} finding(s)</span>
        {["critical", "high", "medium", "low", "info"]
          .filter((sev) => (d.by_severity[sev] ?? 0) > 0)
          .map((sev) => (
            <span key={sev}>{d.by_severity[sev]} {sev}</span>
          ))}
        {d.truncated && (
          <span className="ml-auto">showing the first {d.findings.length}</span>
        )}
      </div>
      <div className="max-h-80 overflow-auto bg-white">
        {d.findings.map((f) => (
          <div key={f.fingerprint} className="flex items-start gap-2 border-b px-2 py-1.5 last:border-b-0">
            <SevBadge sev={f.severity} />
            <div className="min-w-0 flex-1">
              <div className="text-[12px] text-gray-800">
                {f.title}
                {f.is_new && (
                  <span className="ml-1 rounded bg-sky-100 px-1 py-0.5 text-[10px] font-medium text-sky-700">
                    new since last run
                  </span>
                )}
              </div>
              {f.object_name && (
                <div className="truncate text-[11px] text-gray-500" title={f.object_name}>
                  {f.object_name}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ScannersTab({ connectionId }: { connectionId: string | null }) {
  const qc = useQueryClient();
  const [lastRun, setLastRun] = useState<EntraScannerRun[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const q = useQuery({
    queryKey: ["entra-scanners", connectionId],
    queryFn: () => api.entraScanners(connectionId),
  });

  const run = useMutation({
    mutationFn: (ids: string[]) =>
      api.entraRunScanners({ scanner_ids: ids, force: true, notify: false }, connectionId),
    onSuccess: (res, ids) => {
      setLastRun(res.ran);
      setError("");
      // Running one scanner is an explicit "show me what this finds", so open its results.
      if (ids.length === 1) setOpenId(ids[0]);
      qc.invalidateQueries({ queryKey: ["entra-scanners"] });
      qc.invalidateQueries({ queryKey: ["entra-scanner-findings"] });
      qc.invalidateQueries({ queryKey: ["entra-inbox"] });
    },
    onError: (e) => setError(formatError(e)),
  });

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading scanners…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const d = q.data!;
  if (!d.meta.loaded) return <EntraEmpty kind="cold" />;

  const runById = new Map((lastRun || []).map((r) => [r.scanner_id, r]));

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => run.mutate([])}
          disabled={run.isPending}
          className="rounded bg-gray-800 px-3 py-1.5 text-[13px] text-white disabled:opacity-50"
        >
          {run.isPending ? "Running…" : "Run all scanners now"}
        </button>
        <span className="text-[11px] text-gray-500">
          Scanners read the current snapshot — running one never calls Microsoft Graph.
        </span>
        {error && <span className="text-[12px] text-red-600">{error}</span>}
      </div>

      <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900">
        Only <span className="font-medium">new</span> and <span className="font-medium">resolved</span>{" "}
        findings are notified. A digest that repeats findings you already know about trains
        people to filter the sender, and after that nothing gets detected at all.
        {d.always_immediate.length > 0 && (
          <span className="mt-1 block">
            {d.always_immediate.length} signal(s) bypass the digest entirely and always notify
            immediately.
          </span>
        )}
      </div>

      {d.scanners.map((s: EntraScanner) => {
        const result = runById.get(s.id);
        return (
          <div key={s.id} className="rounded-lg border bg-white p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-semibold text-gray-900">{s.name}</span>
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
                {s.cadence}
              </span>
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">
                {s.signal_count} signal(s) · ≥ {s.severity_floor}
              </span>
              {s.blocked ? (
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-800">
                  cannot run
                </span>
              ) : null}
              <button
                onClick={() => run.mutate([s.id])}
                disabled={run.isPending || !!s.blocked}
                className="ml-auto rounded border px-2 py-0.5 text-[12px] text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Run
              </button>
            </div>
            <div className="mt-1 text-[12px] text-gray-600">{s.description}</div>
            {s.blocked && (
              <div className="mt-1 text-[12px] text-amber-800">
                {s.blocked} — reporting zero findings here would be indistinguishable from
                having looked and found nothing.
              </div>
            )}
            <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-500">
              <span>Last run: {s.last_run ? s.last_run.slice(0, 16).replace("T", " ") : "never"}</span>
              {s.last_counts?.total !== undefined && (
                <span>
                  {s.last_counts.total} total · {s.last_counts.new} new ·{" "}
                  {s.last_counts.resolved} resolved
                </span>
              )}
              {!s.blocked && (
                <button
                  onClick={() => setOpenId(openId === s.id ? null : s.id)}
                  className="font-medium text-brand hover:underline"
                >
                  {openId === s.id ? "Hide findings" : "Show findings"}
                </button>
              )}
            </div>
            {result && (
              <div className="mt-2 rounded border border-gray-200 bg-gray-50 px-2 py-1.5 text-[12px]">
                {result.blocked ? (
                  <span className="text-amber-800">{result.blocked}</span>
                ) : (
                  <>
                    {/* Lead with the total. A scanner that has run before reports 0 new
                        forever while sitting on hundreds of open findings, and a summary
                        that only counts the delta reads as "found nothing". */}
                    <span className="font-medium text-gray-800">
                      {result.counts.total} finding(s)
                    </span>
                    <span className="text-gray-600">
                      {result.first_run
                        ? " — first run, recorded as the baseline."
                        : ` — ${result.counts.new} new, ${result.counts.resolved} resolved since the last run.`}
                    </span>
                    {result.immediate.length > 0 && (
                      <span className="ml-1 font-medium text-red-700">
                        {result.immediate.length} bypass the digest and notify immediately.
                      </span>
                    )}
                  </>
                )}
              </div>
            )}
            {openId === s.id && (
              <ScannerFindings
                scannerId={s.id}
                connectionId={connectionId}
                floor={s.severity_floor}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

export function EntraScannersView({ connectionId, onOpenSetup }:
  { connectionId: string | null; onOpenSetup?: () => void }) {
  const [tab, setTab] = useSubTabRoute(TABS.map(([id]) => id), "inbox");
  const statusQ = useQuery({
    queryKey: ["entra-status", connectionId],
    queryFn: () => api.entraStatus(connectionId),
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      {statusQ.data && <CoverageBanner meta={statusQ.data.meta} onOpenSetup={onOpenSetup} />}
      <div className="flex shrink-0 gap-1 border-b bg-white px-3 pt-2">
        {TABS.map(([id, label]) => (
          <button key={id} onClick={() => setTab(id)}
                  className={`rounded-t px-3 py-1.5 text-[13px] ${
                    tab === id ? "border border-b-white bg-white font-medium text-gray-900"
                               : "text-gray-600 hover:text-gray-900"}`}>
            {label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === "inbox" && <InboxTab connectionId={connectionId} />}
        {tab === "scanners" && <div className="h-full overflow-auto"><ScannersTab connectionId={connectionId} /></div>}
        {tab === "hygiene" && <div className="flex h-full min-h-0 flex-col"><IdentityFindingsPanel connectionId={connectionId} /></div>}
      </div>
    </div>
  );
}
