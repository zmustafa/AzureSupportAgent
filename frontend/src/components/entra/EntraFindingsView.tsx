import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type EntraFinding, type EntraSignal } from "../../api";
import { formatError } from "../../utils/format";
import { useDebounced } from "../../utils/perf";
import { EntraEmpty, SEV_STYLE, SevBadge, SortScopeNote, SortTh, useSortState } from "./EntraShared";

/**
 * The findings inbox — every signal's output in one list with persistent workflow state.
 *
 * Suppression state lives server-side in `findings_state.json` and is never rewritten by a
 * collection run: a suppression that vanishes on the next refresh is worse than none.
 */

const SEVERITIES = ["critical", "high", "medium", "low"] as const;

/**
 * Sort columns, matched to the keys `GET /entra/findings` accepts.
 *
 * `""` means "whatever order the server chose", which is what the first render must show —
 * a stored preference is the only thing that should ever override it.
 */
type FindingsSortKey = "" | "severity" | "title" | "object" | "signal" | "state";

export function EntraFindingsView({
  connectionId,
  pillar,
  onOpenSetup,
}: {
  connectionId: string | null;
  pillar?: string;
  onOpenSetup: () => void;
}) {
  const [severity, setSeverity] = useState<string>("");
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 150);
  const [selected, setSelected] = useState<EntraFinding | null>(null);
  // Sorted by the server, not the browser: the request is capped at 500 rows, so a
  // client-side sort would reorder the cap rather than the findings and quietly relabel
  // "the 500 the server picked" as "the top by this column".
  const [sort, setSort] = useSortState<FindingsSortKey>("findings", { key: "", dir: -1 });

  const q = useQuery({
    queryKey: ["entra-findings", connectionId, severity, pillar ?? "", dSearch, sort.key, sort.dir],
    queryFn: () => {
      // Built as a variable rather than inline: api.ts does not yet declare `sort`/`dir`, and
      // TypeScript only excess-property-checks fresh object literals.
      const params = {
        severity: severity || undefined,
        pillar,
        search: dSearch || undefined,
        limit: 500,
        // No key means no parameter, so the server's own default ordering stands untouched.
        ...(sort.key ? { sort: sort.key, dir: sort.dir === 1 ? "asc" : "desc" } : {}),
      };
      return api.entraFindings(params, connectionId);
    },
  });

  const counts = q.data?.by_severity ?? {};
  const signals = q.data?.signals ?? {};

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading findings…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  if (!q.data?.meta.loaded) return <EntraEmpty kind="cold" />;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
        <button
          onClick={() => setSeverity("")}
          className={`rounded px-2 py-1 text-xs font-medium ${!severity ? "bg-gray-800 text-white" : "border text-gray-600"}`}
        >
          All ({q.data.total})
        </button>
        {SEVERITIES.map((s) => (
          <button
            key={s}
            onClick={() => setSeverity(severity === s ? "" : s)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              severity === s ? "bg-gray-800 text-white" : `border ${SEV_STYLE[s].chip}`
            }`}
          >
            {SEV_STYLE[s].label} ({counts[s] ?? 0})
          </button>
        ))}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by object or title…"
          className="ml-auto w-64 rounded border px-2 py-1 text-sm"
        />
        {q.data.suppressed_count > 0 && (
          <span className="text-xs text-gray-400">{q.data.suppressed_count} suppressed</span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {q.data.findings.length === 0 ? (
          <EntraEmpty
            kind="clean"
            detail="No findings match this filter."
            onOpenSetup={onOpenSetup}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="sticky top-0 bg-gray-50">
              <tr className="border-b text-left text-xs text-gray-500">
                <SortTh label="Severity" col="severity" sort={sort} setSort={setSort} className="px-3" />
                <SortTh label="Finding" col="title" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
                <SortTh label="Object" col="object" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
                <SortTh label="Signal" col="signal" sort={sort} setSort={setSort} firstDir={1} className="px-2" />
                <SortTh label="State" col="state" sort={sort} setSort={setSort} className="px-2" />
              </tr>
            </thead>
            <tbody>
              {q.data.findings.map((f) => (
                <tr
                  key={f.fingerprint}
                  onClick={() => setSelected(f)}
                  className="cursor-pointer border-b last:border-b-0 hover:bg-gray-50"
                >
                  <td className="px-3 py-2">
                    <SevBadge sev={f.severity} />
                  </td>
                  <td className="px-2 py-2 text-gray-800">{f.title}</td>
                  <td className="px-2 py-2 text-gray-600">{f.object_name}</td>
                  <td className="px-2 py-2">
                    <code className="rounded bg-gray-100 px-1 text-[11px] text-gray-600">{f.signal_id}</code>
                  </td>
                  <td className="px-2 py-2 text-gray-500">{f.state ?? "open"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <SortScopeNote shown={q.data.findings.length} total={q.data.total} />
      </div>

      {selected && (
        <FindingDrawer
          finding={selected}
          signal={signals[selected.signal_id]}
          connectionId={connectionId}
          onClose={() => setSelected(null)}
          onChanged={() => void q.refetch()}
        />
      )}
    </div>
  );
}

export function FindingDrawer({
  finding,
  signal,
  connectionId,
  onClose,
  onChanged,
}: {
  finding: EntraFinding;
  signal?: EntraSignal;
  connectionId: string | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");

  const evidence = useMemo(() => Object.entries(finding.evidence ?? {}), [finding]);

  const setState = async (state: string) => {
    if (state === "suppressed" && !reason.trim()) return;
    setBusy(true);
    try {
      await api.entraSetFindingState(finding.fingerprint, { state, reason }, connectionId);
      onChanged();
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-y-0 right-0 z-30 w-[32rem] overflow-auto border-l bg-white shadow-xl">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <SevBadge sev={finding.severity} />
          <div className="text-sm font-semibold">Finding</div>
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-700">
          ✕
        </button>
      </div>
      <div className="space-y-4 p-4 text-[13px]">
        <div className="text-base font-semibold text-gray-900">{finding.title}</div>
        <p className="text-gray-700">{finding.detail}</p>

        {signal && (
          <div className="rounded-lg bg-gray-50 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Why this matters</div>
            <p className="mt-1 text-gray-700">{signal.why}</p>
          </div>
        )}

        {/* Evidence is what makes the score verifiable instead of a black box. */}
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Evidence</div>
          <dl className="mt-1 space-y-1">
            {evidence.map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="w-40 shrink-0 text-gray-500">{k}</dt>
                <dd className="min-w-0 flex-1 break-words text-gray-800">
                  {typeof v === "object" ? JSON.stringify(v) : String(v)}
                </dd>
              </div>
            ))}
            {evidence.length === 0 && <div className="text-gray-400">No evidence recorded.</div>}
          </dl>
        </div>

        {signal && signal.remediation_steps.length > 0 && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Remediation</div>
            <p className="mt-1 text-gray-700">{signal.remediation}</p>
            <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-gray-700">
              {signal.remediation_steps.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ol>
          </div>
        )}
        {signal && signal.remediation_steps.length === 0 && signal.remediation && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Remediation</div>
            <p className="mt-1 text-gray-700">{signal.remediation}</p>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {finding.portal_link && (
            <a
              href={finding.portal_link}
              target="_blank"
              rel="noreferrer"
              className="rounded border px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              Open in Entra portal ↗
            </a>
          )}
          {signal?.doc_link && (
            <a
              href={signal.doc_link}
              target="_blank"
              rel="noreferrer"
              className="rounded border px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              Documentation ↗
            </a>
          )}
        </div>

        <div className="border-t pt-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Workflow</div>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              disabled={busy}
              onClick={() => setState("acknowledged")}
              className="rounded border px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              Acknowledge
            </button>
            <button
              disabled={busy || !reason.trim()}
              onClick={() => setState("suppressed")}
              title={reason.trim() ? "" : "A suppression requires a reason"}
              className="rounded border px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              Suppress
            </button>
            <button
              disabled={busy}
              onClick={() => setState("open")}
              className="rounded border px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              Reopen
            </button>
          </div>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (required to suppress)"
            className="mt-2 w-full rounded border px-2 py-1 text-xs"
          />
          <div className="mt-1 text-[11px] text-gray-400">
            Suppressions persist across refreshes and are excluded from the posture score.
          </div>
        </div>
      </div>
    </div>
  );
}
