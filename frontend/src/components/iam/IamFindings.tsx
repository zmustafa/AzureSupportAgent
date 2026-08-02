/** Findings tab — the signal registry's output, plus an honest account of what was not checked.
 *
 * The design constraint that shapes this whole file: a findings screen that shows an empty list
 * is indistinguishable from one where nothing could be measured, and a reader will assume the
 * former. So "unmeasured" is rendered as prominently as the findings, the posture score always
 * carries its coverage, and the grade is genuinely absent below the coverage floor rather than
 * being shown with an asterisk.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IamFinding, type IamPillarScore } from "../../api";
import { useIamConnectionId } from "./IamShared";

const SEV_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-800 border-red-300",
  error: "bg-orange-100 text-orange-800 border-orange-300",
  warning: "bg-amber-100 text-amber-800 border-amber-300",
  info: "bg-sky-100 text-sky-800 border-sky-300",
};

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
  const connectionId = useIamConnectionId();
  const q = useQuery({
    queryKey: ["iam", "score", connectionId ?? ""],
    queryFn: () => api.iamScore(connectionId),
    staleTime: 60 * 1000,
  });
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
  return (
    <div className={`rounded-lg border bg-white p-3 ${f.state === "suppressed" || f.state === "accepted" ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-2">
        <SevChip severity={f.severity} />
        <div className="min-w-0 flex-1">
          <button type="button" onClick={() => setOpen((v) => !v)} className="text-left text-sm font-semibold text-gray-800 hover:underline">
            {f.title}
          </button>
          <div className="truncate text-xs text-gray-500" title={f.subject_label || f.subject}>
            {f.subject_label || f.subject}
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
  const pillars = useMemo<[string, number][]>(
    () => Object.entries(data?.counts_by_pillar ?? {}).filter(([, n]) => n > 0),
    [data],
  );

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
          {pillars.map(([k, n]) => (
            <option key={k} value={k}>{k} ({n})</option>
          ))}
        </select>
        <label className="flex items-center gap-1 text-xs text-gray-600">
          <input type="checkbox" checked={includeSuppressed} onChange={(e) => setIncludeSuppressed(e.target.checked)} />
          Show suppressed
        </label>
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
            <div className="space-y-2">
              {data?.findings.map((f: IamFinding) => (
                <FindingCard key={f.id} f={f} onState={(fp, st) => setState.mutate({ fp, state: st })} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
