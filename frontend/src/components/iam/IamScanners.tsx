/** Scanners tab — the proactive half of the findings engine.
 *
 * A scanner card answers one question: *what changed since this check last looked?* So the
 * things that are structural here, not cosmetic:
 *
 *  - **a blocked scanner never shows a count.** Zero findings and "could not look" are the same
 *    picture and opposite facts, and the reassuring one is wrong. A blocked card shows its
 *    reasons and withholds the number entirely;
 *  - **opening this screen does not consume the delta.** The cards are read with `persist=false`
 *    server-side; only the explicit Run button moves the baseline. Otherwise the first person to
 *    look each morning would turn everyone else's "3 new" into "0 new";
 *  - **unmeasured checks are named even on a card that DID report.** "12 findings" from a
 *    scanner where 5 of its 8 checks could not run is a different fact from "12 findings", and
 *    the card says which;
 *  - **`new` is what matters, `total` is context.** A digest that repeats 400 known findings
 *    trains people to ignore the sender.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IamScannerCard } from "../../api";
import { useIamConnectionId } from "./IamShared";

const SEV_CLASS: Record<string, string> = {
  critical: "bg-red-100 text-red-800 border-red-300",
  error: "bg-orange-100 text-orange-800 border-orange-300",
  warning: "bg-amber-100 text-amber-900 border-amber-300",
  info: "bg-sky-100 text-sky-800 border-sky-300",
};

function Card({ s, onRun, running }: { s: IamScannerCard; onRun: () => void; running: boolean }) {
  const [open, setOpen] = useState(false);
  const blocked = (s.blocked ?? []).length > 0;
  const counts = s.counts;
  return (
    <div className={`rounded-lg border bg-white ${blocked ? "border-amber-300" : ""}`}>
      <div className="flex items-start gap-3 px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="truncate text-sm font-semibold text-gray-800">{s.name}</span>
            <span className="shrink-0 rounded bg-gray-100 px-1.5 text-[10px] uppercase tracking-wide text-gray-600">
              {s.cadence}
            </span>
            <span className="shrink-0 text-[10px] text-gray-500">
              {s.signal_count} check(s) · floor {s.severity_floor}
            </span>
          </div>
          <p className="mt-0.5 text-[11px] text-gray-600">{s.description}</p>
        </div>
        <button
          type="button"
          onClick={onRun}
          disabled={running}
          className="shrink-0 rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
        >
          {running ? "Running…" : "Run now"}
        </button>
      </div>

      {/* A blocked scanner publishes no number at all — not a zero. */}
      {blocked ? (
        <div className="border-t border-amber-300 bg-amber-50 px-3 py-2">
          <div className="text-[11px] font-semibold text-amber-900">
            This scanner could not run — its findings are unknown, not zero
          </div>
          <ul className="mt-1 space-y-0.5">
            {(s.blocked ?? []).map((r) => (
              <li key={r} className="text-[11px] text-amber-900">{r}</li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3 border-t px-3 py-2">
          <Stat label="new" value={counts?.new} tone={counts && counts.new > 0 ? "red" : "gray"} />
          <Stat label="total" value={counts?.total} />
          <Stat label="resolved" value={counts?.resolved} tone="green" />
          <Stat label="known" value={counts?.persisting} />
          <div className="ml-auto flex flex-wrap gap-1">
            {Object.entries(s.by_severity ?? {})
              .filter(([, n]) => n > 0)
              .map(([sev, n]) => (
                <span key={sev} className={`rounded border px-1.5 text-[10px] ${SEV_CLASS[sev] ?? ""}`}>
                  {sev} {n}
                </span>
              ))}
          </div>
        </div>
      )}

      {/* Named even when the scanner reported. A count drawn from 3 of 8 checks is not the same
          fact as a count drawn from 8 of 8, and only this line distinguishes them. */}
      {(s.unmeasured ?? []).length > 0 && (
        <div className="border-t bg-amber-50 px-3 py-1.5">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-[11px] font-medium text-amber-900 hover:underline"
          >
            {open ? "▾" : "▸"} {s.unmeasured!.length} of this scanner's {s.signal_count} check(s)
            could not be performed — these are not passes
          </button>
          {open && (
            <ul className="mt-1 space-y-0.5">
              {s.unmeasured!.map((u) => (
                <li key={u.signal_id} className="text-[11px] text-amber-900">
                  <b>{u.title}</b> — {u.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex items-center gap-3 border-t px-3 py-1 text-[10px] text-gray-500">
        {s.first_run ? (
          <span>
            Never run. The first run records a baseline and deliberately notifies nothing —
            everything would be “new”.
          </span>
        ) : (
          <span>Last run {new Date(s.last_run_at).toLocaleString()}</span>
        )}
        {s.due && !s.first_run && <span className="text-amber-800">· due</span>}
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "gray" }: { label: string; value?: number; tone?: string }) {
  const colour = tone === "red" ? "text-red-600" : tone === "green" ? "text-green-600" : "text-gray-800";
  return (
    <div className="flex items-baseline gap-1">
      {/* An em dash, never 0, when the number is genuinely absent. */}
      <span className={`text-sm font-semibold tabular-nums ${colour}`}>{value ?? "—"}</span>
      <span className="text-[10px] uppercase tracking-wide text-gray-500">{label}</span>
    </div>
  );
}

export function ScannersTab() {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [ran, setRan] = useState<string>("");

  const q = useQuery({
    queryKey: ["iam", "scanners", connectionId ?? ""],
    queryFn: () => api.iamScanners(connectionId),
    staleTime: 60 * 1000,
  });

  const runOne = useMutation({
    mutationFn: (id: string) => api.iamRunScanner(id, connectionId),
    onSuccess: (_d, id) => {
      setRan(id);
      void qc.invalidateQueries({ queryKey: ["iam", "scanners"] });
    },
  });

  const runAll = useMutation({
    mutationFn: () => api.iamRunScanners(true, connectionId),
    onSuccess: () => {
      setRan("all");
      void qc.invalidateQueries({ queryKey: ["iam", "scanners"] });
    },
  });

  const scanners = q.data?.scanners ?? [];
  const blockedCount = scanners.filter((s) => (s.blocked ?? []).length > 0).length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
        <span className="text-sm font-medium text-gray-700">Scanners</span>
        <span className="text-[11px] text-gray-600">
          A scanner is a named selection of the checks on the Findings tab, with a cadence and a
          severity floor. Running one records a baseline so the next run can report what changed.
        </span>
        {blockedCount > 0 && (
          <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-900">
            {blockedCount} blocked
          </span>
        )}
        <button
          type="button"
          onClick={() => runAll.mutate()}
          disabled={runAll.isPending}
          className="ml-auto rounded bg-gray-800 px-2 py-1 text-xs text-white hover:bg-gray-700 disabled:opacity-50"
        >
          {runAll.isPending ? "Running…" : "Run all now"}
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {q.isLoading && <div className="text-sm text-gray-500">Loading scanners…</div>}
        {ran && !runAll.isPending && !runOne.isPending && (
          <div className="mb-2 rounded border border-green-300 bg-green-50 px-3 py-1.5 text-[11px] text-green-900">
            Baseline recorded. The counts below now describe changes since that run, and any new
            findings have been delivered to the notification center.
          </div>
        )}
        <div className="space-y-2">
          {scanners.map((s) => (
            <Card
              key={s.id}
              s={s}
              running={runOne.isPending && runOne.variables === s.id}
              onRun={() => runOne.mutate(s.id)}
            />
          ))}
        </div>
        {!q.isLoading && scanners.length === 0 && (
          <div className="rounded border bg-white p-3 text-xs text-gray-600">
            No scanners are registered.
          </div>
        )}
      </div>
    </div>
  );
}
