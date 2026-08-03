/** Findings tab — the signal registry's output, plus an honest account of what was not checked.
 *
 * The design constraint that shapes this whole file: a findings screen that shows an empty list
 * is indistinguishable from one where nothing could be measured, and a reader will assume the
 * former. So "unmeasured" is rendered as prominently as the findings, the posture score always
 * carries its coverage, and the grade is genuinely absent below the coverage floor rather than
 * being shown with an asterisk.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IamFinding, type IamPillarScore } from "../../api";
import { usePersistedState } from "../../utils/persistedState";
import { InvestigateLink, investigatableId } from "../entra/InvestigateLink";
import { useIamConnectionId } from "./IamShared";

const SEV_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-800 border-red-300",
  error: "bg-orange-100 text-orange-800 border-orange-300",
  warning: "bg-amber-100 text-amber-800 border-amber-300",
  info: "bg-sky-100 text-sky-800 border-sky-300",
};

const SEV_RANK: Record<string, number> = { critical: 0, error: 1, warning: 2, info: 3 };

type GroupKey = "none" | "pillar" | "severity" | "signal" | "object_kind" | "state";

const GROUP_OPTIONS: { id: GroupKey; label: string }[] = [
  { id: "none", label: "No grouping" },
  { id: "severity", label: "Group by severity" },
  { id: "pillar", label: "Group by pillar" },
  { id: "signal", label: "Group by check" },
  { id: "object_kind", label: "Group by affected object" },
  { id: "state", label: "Group by state" },
];

// The second level. One check fires once per affected subject, so a severity section on a real
// tenant is mostly the SAME finding repeated against different principals — the screenshot that
// prompted this had "Guest holds privileged access" twice in a row under Critical. Grouping
// again by check turns that back into one line with a count.
const SUB_GROUP_OPTIONS: { id: GroupKey; label: string }[] = [
  { id: "none", label: "then flat" },
  { id: "signal", label: "then by check" },
  { id: "pillar", label: "then by pillar" },
  { id: "severity", label: "then by severity" },
  { id: "object_kind", label: "then by affected object" },
  { id: "state", label: "then by state" },
];

// Which server-side count map is authoritative for each grouping. There is one per mode on
// purpose: the page is capped, so counting the rendered array would understate every group the
// moment an estate exceeds the page size, and a header count that shrinks as you scroll is
// worse than no header count at all.
//
// This only works for the TOP level. A second level is a (primary, secondary) PAIR, and the
// server publishes one map per single dimension — there is no honest pair count available, so
// sub-group counts are counted from the page and are labelled as such whenever the section they
// sit in was truncated. See `subCountLabel`.
const COUNT_FIELD: Record<Exclude<GroupKey, "none">, "counts_by_pillar" | "counts_by_severity" | "counts_by_signal" | "counts_by_object_kind" | "counts_by_state"> = {
  pillar: "counts_by_pillar",
  severity: "counts_by_severity",
  signal: "counts_by_signal",
  object_kind: "counts_by_object_kind",
  state: "counts_by_state",
};

function groupKeyOf(f: IamFinding, by: Exclude<GroupKey, "none">): string {
  return by === "pillar" ? f.pillar : by === "severity" ? f.severity : by === "signal" ? f.signal_id : by === "object_kind" ? f.object_kind : f.state;
}

function titleCase(s: string) {
  return s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** Shared so the score card and the grouped list read the SAME cached response. The pillar
 *  labels and — more importantly — the pillar *states* live here, and a group header that
 *  disagreed with the score grid above it would be its own bug. */
function useIamScoreQuery() {
  const connectionId = useIamConnectionId();
  return useQuery({
    queryKey: ["iam", "score", connectionId ?? ""],
    queryFn: () => api.iamScore(connectionId),
    staleTime: 60 * 1000,
  });
}

const STATE_STYLE: Record<string, string> = {
  open: "bg-white text-gray-600 border-gray-300",
  in_progress: "bg-blue-50 text-blue-700 border-blue-300",
  suppressed: "bg-gray-100 text-gray-500 border-gray-300",
  accepted: "bg-purple-50 text-purple-700 border-purple-300",
};

const PILLAR_STATE_STYLE: Record<string, string> = {
  ok: "text-emerald-700",
  partial: "text-amber-700",
  blind: "text-gray-500",
  not_implemented: "text-gray-400",
};

function SevChip({ severity }: { severity: string }) {
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${SEV_STYLE[severity] ?? SEV_STYLE.info}`}>
      {severity}
    </span>
  );
}

/** The score, always with its coverage, and with the grade withheld when it would mislead. */
function ScoreCard() {
  const q = useIamScoreQuery();
  const s = q.data;
  if (q.isLoading) return <div className="rounded-lg border bg-white p-3 text-sm text-gray-500">Loading score…</div>;
  if (!s) return null;

  const pct = Math.round((s.coverage ?? 0) * 100);
  const floorPct = Math.round((s.min_coverage_for_grade ?? 0.6) * 100);

  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="flex items-start gap-4">
        <div className="text-center">
          {s.grade ? (
            <>
              <div className="text-4xl font-bold tabular-nums text-gray-800">{s.grade}</div>
              <div className="text-[11px] text-gray-500">{s.grade_label}</div>
            </>
          ) : (
            // Deliberately not a letter, not a dash-in-a-circle that reads like a bad grade.
            <div className="max-w-[9rem] text-xs text-gray-500">
              <div className="mb-1 text-lg font-semibold text-gray-400">No grade</div>
              {s.grade_withheld_reason}
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold tabular-nums text-gray-800">{s.score ?? "—"}</span>
            <span className="text-xs text-gray-500">/ 100 posture score</span>
          </div>
          <div className="mt-1 text-xs text-gray-600">
            Measured <b className="tabular-nums">{pct}%</b> of the weighted checks
            {pct < floorPct && <span className="text-amber-700"> · below the {floorPct}% needed for a grade</span>}
          </div>
          <div className="mt-1 h-2 w-full rounded bg-gray-100" title={`${pct}% coverage`}>
            <div className={`h-2 rounded ${pct < floorPct ? "bg-amber-400" : "bg-emerald-500"}`} style={{ width: `${pct}%` }} />
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-4">
        {(s.pillars ?? []).map((p: IamPillarScore) => (
          <div key={p.key} className="text-xs" title={p.reason || p.desc}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-gray-600">{p.label}</span>
              <span className={`font-semibold tabular-nums ${PILLAR_STATE_STYLE[p.state] ?? ""}`}>
                {/* null score renders as its state, never as 0 or 100. */}
                {p.score === null
                  ? p.state === "not_implemented"
                    ? "not built"
                    : "not measured"
                  : p.score}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** What the scan could not look at. Given equal billing to the findings themselves. */
function UnmeasuredPanel({ items }: { items: { signal_id: string; title: string; reason: string }[] }) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-3">
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 text-left">
        <span className="text-sm font-semibold text-amber-900">
          {items.length} check{items.length === 1 ? "" : "s"} could not be performed
        </span>
        <span className="text-xs text-amber-700">— these are not passes</span>
        <span className="ml-auto text-xs text-amber-700">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <ul className="mt-2 space-y-1">
          {items.map((u) => (
            <li key={u.signal_id} className="text-xs text-amber-900">
              <b>{u.title}</b> — {u.reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingCard({ f, onState }: { f: IamFinding; onState: (fp: string, state: string) => void }) {
  const [open, setOpen] = useState(false);
  // Shared with every other surface that offers this jump, so the rules for "is this a
  // principal we can actually resolve" cannot drift apart screen by screen.
  const principalId = investigatableId(f.object_kind, f.subject);
  return (
    <div data-testid="finding-card" className={`rounded-lg border bg-white p-3 ${f.state === "suppressed" || f.state === "accepted" ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-2">
        <SevChip severity={f.severity} />
        <div className="min-w-0 flex-1">
          <button type="button" onClick={() => setOpen((v) => !v)} className="text-left text-sm font-semibold text-gray-800 hover:underline">
            {f.title}
          </button>
          <div className="flex items-center gap-1">
            <div className="min-w-0 truncate text-xs text-gray-500" title={f.subject_label || f.subject}>
              {f.subject_label || f.subject}
            </div>
            {principalId && (
              <InvestigateLink
                principalId={principalId}
                label={f.subject_label || f.subject}
                title="Investigate this identity — everything we know about it"
              />
            )}
          </div>
          <div className="mt-1 text-xs text-gray-700">{f.detail}</div>
        </div>
        {f.count > 1 && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] tabular-nums text-gray-600">{f.count}</span>}
        <span className={`rounded border px-1.5 py-0.5 text-[10px] ${STATE_STYLE[f.state] ?? STATE_STYLE.open}`}>{f.state.replace("_", " ")}</span>
      </div>
      {open && (
        <div className="mt-3 space-y-2 border-t pt-2 text-xs">
          {f.why && (
            <div>
              <div className="font-semibold text-gray-700">Why this matters</div>
              <div className="text-gray-600">{f.why}</div>
            </div>
          )}
          <div>
            <div className="font-semibold text-gray-700">What to do</div>
            <div className="text-gray-600">{f.remediation}</div>
          </div>
          {f.frameworks.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {f.frameworks.map((fr) => (
                <span key={fr} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{fr}</span>
              ))}
            </div>
          )}
          {Object.keys(f.evidence ?? {}).length > 0 && (
            <div>
              <div className="font-semibold text-gray-700">Evidence</div>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-gray-50 p-2 text-[11px] text-gray-700">
                {JSON.stringify(f.evidence, null, 2)}
              </pre>
            </div>
          )}
          {f.state_reason && (
            <div className="text-gray-500">
              {f.state} by {f.state_updated_by || "unknown"}: {f.state_reason}
            </div>
          )}
          <div className="flex flex-wrap gap-1 pt-1">
            {["open", "in_progress", "accepted", "suppressed"].map((st) => (
              <button
                key={st}
                type="button"
                disabled={st === f.state}
                onClick={() => onState(f.id, st)}
                className="rounded border px-2 py-0.5 text-[11px] text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                {st.replace("_", " ")}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function FindingsTab() {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [severity, setSeverity] = useState("");
  const [pillar, setPillar] = useState("");
  const [includeSuppressed, setIncludeSuppressed] = useState(false);
  // Grouped by severity and folded shut by default. A flat list of 122 findings opens on
  // whichever three happen to sort first and buries the shape of the problem; the collapsed
  // headers ARE the summary — how many criticals, how many errors, worst first — and opening
  // one is a deliberate act. Persisted, so a reader who prefers the flat list keeps it.
  const [groupBy, setGroupBy] = usePersistedState<GroupKey>("iam.findings.groupBy", "severity");
  // Second level. A check fires once per affected subject, so "Critical" on a real tenant is
  // largely one check repeated — by check, those collapse to a single line with a count.
  const [subGroupBy, setSubGroupBy] = usePersistedState<GroupKey>("iam.findings.subGroupBy", "signal");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // Which grouping the automatic collapse has already been applied for. Without it, every
  // refetch (a severity filter, a state change, a background refresh) would re-fold the
  // section the reader had just opened.
  const autoCollapsedFor = useRef<string>("");

  // A collapse key means nothing under a different grouping — carrying the set across would
  // start the next grouping with arbitrary sections already folded shut.
  useEffect(() => {
    setCollapsed(new Set());
    autoCollapsedFor.current = "";
  }, [groupBy, subGroupBy]);

  // Selecting the same dimension twice would nest every section inside a single child of
  // itself. Fall back to flat rather than rendering that.
  const effectiveSub: GroupKey = subGroupBy === groupBy ? "none" : subGroupBy;

  const q = useQuery({
    queryKey: ["iam", "findings", severity, pillar, includeSuppressed, connectionId ?? ""],
    queryFn: () =>
      api.iamFindings({
        severity: severity || undefined,
        pillar: pillar || undefined,
        include_suppressed: includeSuppressed || undefined,
        connection_id: connectionId,
      }),
    staleTime: 60 * 1000,
  });

  const scoreQ = useIamScoreQuery();
  // Only fetched when the reader actually groups by check — the catalogue is static and its
  // titles are the only thing that can name a signal group properly.
  const catalogQ = useQuery({
    queryKey: ["iam", "signals"],
    queryFn: () => api.iamSignals(),
    enabled: groupBy === "signal" || effectiveSub === "signal",
    staleTime: Infinity,
  });

  const setState = useMutation({
    mutationFn: ({ fp, state }: { fp: string; state: string }) =>
      api.iamSetFindingState(fp, state, "", connectionId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["iam", "findings"] });
      void qc.invalidateQueries({ queryKey: ["iam", "score"] });
    },
  });

  const data = q.data;
  const counts = data?.counts_by_severity ?? {};
  const scorePillars = useMemo(() => scoreQ.data?.pillars ?? [], [scoreQ.data]);
  const pillarLabel = useMemo(() => {
    const m = new Map<string, string>();
    scorePillars.forEach((p) => m.set(p.key, p.label));
    return m;
  }, [scorePillars]);
  const pillars = useMemo<[string, number][]>(
    () => Object.entries(data?.counts_by_pillar ?? {}).filter(([, n]) => n > 0),
    [data],
  );

  const signalTitle = useMemo(() => {
    const m = new Map<string, string>();
    (catalogQ.data?.signals ?? []).forEach((s) => m.set(s.id, s.title));
    return m;
  }, [catalogQ.data]);

  // Label for a bucket key under a given dimension. Shared by both levels so a check reads the
  // same whether it is a section or a sub-section.
  const labelFor = useMemo(
    () => (by: Exclude<GroupKey, "none">, key: string, sample?: IamFinding) =>
      by === "pillar" ? (pillarLabel.get(key) || key)
      : by === "signal" ? (signalTitle.get(key) || sample?.title || key)
      : titleCase(key),
    [pillarLabel, signalTitle],
  );

  const groups = useMemo(() => {
    if (groupBy === "none" || !data) return null;
    const by = groupBy;
    const trueCounts = data[COUNT_FIELD[by]] ?? {};
    const buckets = new Map<string, IamFinding[]>();
    for (const f of data.findings) {
      const k = groupKeyOf(f, by);
      const list = buckets.get(k);
      if (list) list.push(f);
      else buckets.set(k, [f]);
    }

    /** Second level, counted from the PAGE.
     *
     * The server publishes one count map per single dimension, so there is no authoritative
     * count for a (severity, check) pair. That is fine while the section is complete — page
     * counts are then exact — and the caller marks the section when it is not, rather than
     * printing a number that quietly means something narrower than it appears. */
    const subdivide = (items: IamFinding[]) => {
      if (effectiveSub === "none" || items.length < 2) return null;
      const sub = new Map<string, IamFinding[]>();
      for (const f of items) {
        const k = groupKeyOf(f, effectiveSub);
        const list = sub.get(k);
        if (list) list.push(f);
        else sub.set(k, [f]);
      }
      // Nothing gained by nesting when every child holds one finding — that is the same list
      // with an extra click in front of it.
      if (sub.size === items.length) return null;
      return [...sub.entries()]
        .map(([key, subItems]) => ({
          key,
          label: labelFor(effectiveSub, key, subItems[0]),
          items: subItems,
          worst: Math.min(...subItems.map((f) => SEV_RANK[f.severity] ?? 3)),
          rowsShown: subItems.reduce((a, f) => a + f.count, 0),
        }))
        .sort((a, b) => a.worst - b.worst || b.items.length - a.items.length || a.label.localeCompare(b.label));
    };

    const out = [...buckets.entries()].map(([key, items]) => ({
      key,
      label: labelFor(by, key, items[0]),
      items,
      sub: subdivide(items),
      // The page is sorted worst-first globally, so the first member present is the worst one
      // on this page. `total` is the honest size; `items.length` is only what was paged in.
      worst: Math.min(...items.map((f) => SEV_RANK[f.severity] ?? 3)),
      total: trueCounts[key] ?? items.length,
      rowsShown: items.reduce((a, f) => a + f.count, 0),
      note: "",
    }));

    if (by === "pillar") {
      // Registry weight order, so the section order matches the score grid above.
      const order = new Map(scorePillars.map((p, i) => [p.key, i]));
      out.sort((a, b) => (order.get(a.key) ?? 99) - (order.get(b.key) ?? 99));
      // A pillar that produced nothing BECAUSE it could not be measured must still appear.
      // Omitting it renders exactly like a pillar that was checked and came back clean, which
      // is the single claim this screen is built never to make by accident.
      for (const p of scorePillars) {
        if (buckets.has(p.key)) continue;
        if (p.state !== "blind" && p.state !== "not_implemented") continue;
        out.push({
          key: p.key,
          label: p.label,
          items: [],
          sub: null,
          worst: 9,
          total: 0,
          rowsShown: 0,
          note: p.state === "not_implemented" ? "not built — no check exists for this pillar yet" : "not measured — the inputs for these checks were not collected",
        });
      }
    } else {
      out.sort((a, b) => a.worst - b.worst || b.total - a.total || a.label.localeCompare(b.label));
    }
    return out;
  }, [data, groupBy, effectiveSub, labelFor, scorePillars]);

  // Every collapsible key on screen, both levels. Used by Collapse all / Expand all and by the
  // initial fold, so neither can leave half the tree in a state the buttons do not control.
  const allGroupKeys = useMemo(
    () =>
      (groups ?? []).flatMap((g) => [g.key, ...(g.sub ?? []).map((s) => `${g.key}::${s.key}`)]),
    [groups],
  );

  const toggleGroup = (key: string) =>
    setCollapsed((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });

  // Fold everything the first time a grouping produces sections. Guarded by the ref so a
  // refetch cannot re-fold what the reader just opened, and keyed by the grouping so switching
  // dimensions starts folded again.
  useEffect(() => {
    if (groupBy === "none" || allGroupKeys.length === 0) return;
    const signature = `${groupBy}|${effectiveSub}`;
    if (autoCollapsedFor.current === signature) return;
    autoCollapsedFor.current = signature;
    setCollapsed(new Set(allGroupKeys));
  }, [allGroupKeys, groupBy, effectiveSub]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
        <span className="text-sm font-medium text-gray-700">Findings</span>
        {(["critical", "error", "warning", "info"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSeverity(severity === s ? "" : s)}
            className={`rounded border px-2 py-0.5 text-xs ${severity === s ? SEV_STYLE[s] : "border-gray-300 bg-white text-gray-600 hover:bg-gray-50"}`}
          >
            {/* An em dash, not 0, until the count is actually known. This screen can take tens
                of seconds on a large tenant, and "critical 0" beside "Loading findings…" is the
                most reassuring possible rendering of "we have not finished looking". */}
            {s} <span className="tabular-nums">{q.isLoading ? "—" : (counts[s] ?? 0)}</span>
          </button>
        ))}
        <select
          value={pillar}
          onChange={(e) => setPillar(e.target.value)}
          className="rounded border border-gray-300 px-2 py-0.5 text-xs"
          aria-label="Filter by pillar"
        >
          <option value="">All pillars</option>
          {/* The registry key (`priv`, `byp`, `dp`) is an internal identifier, not a label a
              reader can act on. Fall back to it only if the score has not loaded yet. */}
          {pillars.map(([k, n]) => (
            <option key={k} value={k}>{pillarLabel.get(k) || k} ({n})</option>
          ))}
        </select>
        <select
          value={groupBy}
          onChange={(e) => setGroupBy(e.target.value as GroupKey)}
          className="rounded border border-gray-300 px-2 py-0.5 text-xs"
          aria-label="Group findings"
        >
          {GROUP_OPTIONS.map((o) => (
            <option key={o.id} value={o.id}>{o.label}</option>
          ))}
        </select>
        {groupBy !== "none" && (
          <select
            value={subGroupBy}
            onChange={(e) => setSubGroupBy(e.target.value as GroupKey)}
            className="rounded border border-gray-300 px-2 py-0.5 text-xs"
            aria-label="Sub-group findings"
          >
            {/* The primary dimension is omitted: nesting a section inside itself yields one
                child containing everything, which is a click with no information in it. */}
            {SUB_GROUP_OPTIONS.filter((o) => o.id !== groupBy).map((o) => (
              <option key={o.id} value={o.id}>{o.label}</option>
            ))}
          </select>
        )}
        <label className="flex items-center gap-1 text-xs text-gray-600">
          <input type="checkbox" checked={includeSuppressed} onChange={(e) => setIncludeSuppressed(e.target.checked)} />
          Show suppressed
        </label>
        {groups && groups.length > 1 && (
          <div className="flex gap-1">
            <button
              type="button"
              onClick={() => setCollapsed(new Set(allGroupKeys))}
              className="rounded border border-gray-300 bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50"
            >
              Collapse all
            </button>
            <button
              type="button"
              onClick={() => setCollapsed(new Set())}
              className="rounded border border-gray-300 bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50"
            >
              Expand all
            </button>
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
        <ScoreCard />
        <UnmeasuredPanel items={data?.unmeasured ?? []} />
        {q.isLoading ? (
          <div className="text-sm text-gray-500">Loading findings…</div>
        ) : (data?.findings.length ?? 0) === 0 ? (
          // Never "all clear" — the unmeasured panel above may be the real story.
          <div className="rounded-lg border bg-white p-4 text-sm text-gray-600">
            No findings matched.{" "}
            {(data?.unmeasured.length ?? 0) > 0
              ? "Note that some checks could not be performed — see above."
              : "Run an access scan or seed demo data if you expected results."}
          </div>
        ) : (
          <>
            {data?.truncated && (
              <div className="rounded border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-900">
                Showing the first {data.limit} of {data.total} findings.
              </div>
            )}
            {groups ? (
              <div className="space-y-3">
                {groups.map((g) => (
                  <div key={g.key} data-testid="finding-group" data-group={g.key}>
                    <button
                      type="button"
                      onClick={() => g.items.length > 0 && toggleGroup(g.key)}
                      className="flex w-full items-center gap-2 rounded-t border border-b-0 bg-gray-50 px-3 py-1.5 text-left"
                    >
                      <span className="w-3 shrink-0 text-[10px] text-gray-400">
                        {g.items.length === 0 ? "" : collapsed.has(g.key) ? "▸" : "▾"}
                      </span>
                      {g.worst <= 3 && <SevChip severity={["critical", "error", "warning", "info"][g.worst]} />}
                      <span className="truncate text-sm font-semibold text-gray-800">{g.label}</span>
                      {g.note ? (
                        // Never a "0": a pillar nobody could measure has not passed, and a zero
                        // beside its name is exactly how it would read.
                        <span className="truncate text-[11px] text-amber-700">{g.note}</span>
                      ) : (
                        <>
                          <span className="shrink-0 rounded bg-white px-1.5 py-0.5 text-[11px] tabular-nums text-gray-600">
                            {g.total}
                          </span>
                          {/* The count above is the true size of the group for the current
                              filter. Say so explicitly when the page could not carry it all. */}
                          {g.items.length < g.total && (
                            <span className="shrink-0 text-[11px] text-amber-700">showing {g.items.length}</span>
                          )}
                          <span className="ml-auto shrink-0 text-[11px] text-gray-400">
                            {g.rowsShown} affected row{g.rowsShown === 1 ? "" : "s"}
                          </span>
                        </>
                      )}
                    </button>
                    {!collapsed.has(g.key) && g.items.length > 0 && (
                      <div className="space-y-2 rounded-b border border-t-0 bg-gray-50/50 p-2">
                        {g.sub
                          ? g.sub.map((s) => {
                              const subKey = `${g.key}::${s.key}`;
                              // A sub-group of one is the finding itself. Rendering a header
                              // over a single card invents a hierarchy that is not there and
                              // puts a click in front of the thing the reader came to read.
                              if (s.items.length === 1) {
                                return (
                                  <FindingCard
                                    key={s.items[0].id}
                                    f={s.items[0]}
                                    onState={(fp, st) => setState.mutate({ fp, state: st })}
                                  />
                                );
                              }
                              return (
                                <div key={subKey} data-testid="finding-subgroup" data-group={subKey}>
                                  <button
                                    type="button"
                                    onClick={() => toggleGroup(subKey)}
                                    className="flex w-full items-center gap-2 rounded border bg-white px-3 py-1 text-left"
                                  >
                                    <span className="w-3 shrink-0 text-[10px] text-gray-400">
                                      {collapsed.has(subKey) ? "▸" : "▾"}
                                    </span>
                                    {s.worst <= 3 && <SevChip severity={["critical", "error", "warning", "info"][s.worst]} />}
                                    <span className="truncate text-[13px] font-medium text-gray-800">{s.label}</span>
                                    {/* Counted from the page, not from a server tally — there is
                                        no authoritative count for a (section, sub-section) pair.
                                        Exact while the section is whole, and the section header
                                        above already says when it is not. */}
                                    <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[11px] tabular-nums text-gray-600">
                                      {g.items.length < g.total ? `${s.items.length} shown` : s.items.length}
                                    </span>
                                    <span className="ml-auto shrink-0 text-[11px] text-gray-400">
                                      {s.rowsShown} affected row{s.rowsShown === 1 ? "" : "s"}
                                    </span>
                                  </button>
                                  {!collapsed.has(subKey) && (
                                    <div className="mt-1 space-y-2 pl-4">
                                      {s.items.map((f) => (
                                        <FindingCard key={f.id} f={f} onState={(fp, st) => setState.mutate({ fp, state: st })} />
                                      ))}
                                    </div>
                                  )}
                                </div>
                              );
                            })
                          : g.items.map((f) => (
                              <FindingCard key={f.id} f={f} onState={(fp, st) => setState.mutate({ fp, state: st })} />
                            ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-2">
                {data?.findings.map((f: IamFinding) => (
                  <FindingCard key={f.id} f={f} onState={(fp, st) => setState.mutate({ fp, state: st })} />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
