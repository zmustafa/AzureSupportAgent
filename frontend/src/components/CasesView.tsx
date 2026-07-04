import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  type CaseDetail,
  type CaseFile,
  type CaseListResp,
  type CaseStatus,
  type CaseTimelineEvent,
} from "../api";
import { Skeleton } from "../utils/perf";
import { useConfirm } from "./ConfirmDialog";

// ---------------------------------------------------------------- status / severity styling
const STATUS_ORDER: CaseStatus[] = [
  "open", "investigating", "remediating", "verifying", "resolved", "closed",
];
const STATUS_META: Record<CaseStatus, { label: string; chip: string; dot: string }> = {
  open: { label: "Open", chip: "bg-red-50 text-red-700 border-red-200", dot: "bg-red-500" },
  investigating: { label: "Investigating", chip: "bg-amber-50 text-amber-700 border-amber-200", dot: "bg-amber-500" },
  remediating: { label: "Remediating", chip: "bg-blue-50 text-blue-700 border-blue-200", dot: "bg-blue-500" },
  verifying: { label: "Verifying", chip: "bg-violet-50 text-violet-700 border-violet-200", dot: "bg-violet-500" },
  resolved: { label: "Resolved", chip: "bg-green-50 text-green-700 border-green-200", dot: "bg-green-500" },
  closed: { label: "Closed", chip: "bg-gray-100 text-gray-500 border-gray-200", dot: "bg-gray-400" },
};
const SEV_META: Record<string, { label: string; tone: string }> = {
  info: { label: "Info", tone: "text-gray-500" },
  warning: { label: "Warning", tone: "text-amber-600" },
  error: { label: "Error", tone: "text-orange-600" },
  critical: { label: "Critical", tone: "text-red-600" },
};
const EVENT_META: Record<string, { icon: string; tone: string }> = {
  opened: { icon: "🟢", tone: "text-green-700" },
  status: { icon: "🔄", tone: "text-gray-600" },
  note: { icon: "📝", tone: "text-gray-600" },
  attach: { icon: "📎", tone: "text-blue-600" },
  investigation: { icon: "🔬", tone: "text-violet-600" },
  handoff: { icon: "🤝", tone: "text-blue-600" },
  assigned: { icon: "👤", tone: "text-gray-600" },
  resolved: { icon: "✅", tone: "text-green-700" },
  reopened: { icon: "↩️", tone: "text-amber-700" },
};

function fmt(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
function rel(ts: string): string {
  const d = new Date(ts).getTime();
  if (Number.isNaN(d)) return "";
  const s = Math.round((Date.now() - d) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function StatusChip({ status }: { status: CaseStatus }) {
  const m = STATUS_META[status] ?? STATUS_META.open;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${m.chip}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

// ================================================================ list view
function CaseList() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [openOnly, setOpenOnly] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState("warning");

  const q = useQuery<CaseListResp>({
    queryKey: ["cases", { openOnly }],
    queryFn: () => api.cases({ openOnly }),
    staleTime: 15_000,
  });

  const create = useMutation({
    mutationFn: () => api.createCase({ title: title.trim(), severity }),
    onSuccess: (c) => {
      setTitle("");
      setShowNew(false);
      qc.invalidateQueries({ queryKey: ["cases"] });
      nav(`/cases/${c.id}`);
    },
  });

  const data = q.data;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              <span>🗂️</span> Case Files
            </h1>
            <p className="mt-0.5 max-w-3xl text-[13px] text-gray-500">
              The durable spine of an incident. Each case threads findings → investigation →
              evidence → remediation → verification onto one append-only timeline that{" "}
              <span className="font-medium text-gray-700">survives a refresh and reassignment</span>
              {" "}— no more losing the War Room when the tab closes.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-[12px] text-gray-600">
              <input
                type="checkbox"
                checked={openOnly}
                onChange={(e) => setOpenOnly(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-gray-300"
              />
              Open only
            </label>
            <button
              onClick={() => setShowNew((v) => !v)}
              className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[13px] font-medium text-blue-700 hover:bg-blue-100"
            >
              + New case
            </button>
            <button
              onClick={() => q.refetch()}
              disabled={q.isFetching}
              className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {q.isFetching ? "Loading…" : "Refresh"}
            </button>
          </div>
        </div>

        {showNew && (
          <div className="mt-3 flex flex-wrap items-end gap-2 rounded-lg border bg-gray-50 p-3">
            <div className="min-w-[260px] flex-1">
              <label className="mb-0.5 block text-[11px] font-medium text-gray-600">Title</label>
              <input
                autoFocus
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && title.trim()) create.mutate(); }}
                placeholder="e.g. Front Door 5xx spike on checkout"
                className="w-full rounded-lg border px-3 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="mb-0.5 block text-[11px] font-medium text-gray-600">Severity</label>
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className="rounded-lg border px-3 py-1.5 text-sm"
              >
                {Object.entries(SEV_META).map(([k, v]) => (
                  <option key={k} value={k}>{v.label}</option>
                ))}
              </select>
            </div>
            <button
              onClick={() => create.mutate()}
              disabled={!title.trim() || create.isPending}
              className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {create.isPending ? "Opening…" : "Open case"}
            </button>
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        {q.isLoading ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-14 w-full" />)}
            </div>
            <Skeleton className="h-64 w-full" />
          </div>
        ) : q.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            Couldn&apos;t load case files. You may not have the{" "}
            <code className="rounded bg-red-100 px-1">cases.read</code> permission.
            <button onClick={() => q.refetch()} className="ml-2 underline">Retry</button>
          </div>
        ) : !data || data.cases.length === 0 ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            {openOnly ? "No open cases. " : "No cases yet. "}
            Open one above, or escalate an investigation into a case from the War Room.
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="Total" value={data.summary.total} />
              <Stat label="Open" value={data.summary.open} tone={data.summary.open ? "text-red-700" : "text-green-700"} />
            </div>

            <div className="overflow-hidden rounded-lg border bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-gray-50 text-left text-[12px] text-gray-600">
                    <th className="px-3 py-2 font-medium">Case</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Severity</th>
                    <th className="px-3 py-2 font-medium">Workload</th>
                    <th className="px-3 py-2 font-medium">Assignee</th>
                    <th className="px-3 py-2 text-right font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {data.cases.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => nav(`/cases/${c.id}`)}
                      className="cursor-pointer border-t hover:bg-blue-50/40"
                    >
                      <td className="max-w-[360px] px-3 py-2">
                        <div className="truncate font-medium text-gray-900">{c.title}</div>
                        {c.summary && <div className="truncate text-[11px] text-gray-500">{c.summary}</div>}
                      </td>
                      <td className="px-3 py-2"><StatusChip status={c.status} /></td>
                      <td className={`px-3 py-2 text-[12px] font-medium ${SEV_META[c.severity]?.tone ?? "text-gray-500"}`}>
                        {SEV_META[c.severity]?.label ?? c.severity}
                      </td>
                      <td className="max-w-[160px] truncate px-3 py-2 text-[12px] text-gray-600">
                        {c.workload_name || "—"}
                      </td>
                      <td className="max-w-[140px] truncate px-3 py-2 text-[12px] text-gray-600">
                        {c.assignee || "—"}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-[11px] text-gray-400" title={fmt(c.updated_at)}>
                        {rel(c.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ================================================================ detail view
function CaseDetailView({ caseId }: { caseId: string }) {
  const nav = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [note, setNote] = useState("");

  const q = useQuery<CaseDetail>({
    queryKey: ["case", caseId],
    queryFn: () => api.getCase(caseId),
    staleTime: 10_000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["case", caseId] });
    qc.invalidateQueries({ queryKey: ["cases"] });
  };

  const setStatus = useMutation({
    mutationFn: (status: CaseStatus) => api.updateCase(caseId, { status }),
    onSuccess: invalidate,
  });
  const addNote = useMutation({
    mutationFn: () => api.addCaseNote(caseId, note.trim()),
    onSuccess: () => { setNote(""); invalidate(); },
  });
  const del = useMutation({
    mutationFn: () => api.deleteCase(caseId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cases"] });
      nav("/cases");
    },
  });

  if (q.isLoading) {
    return (
      <div className="flex min-w-0 flex-1 flex-col overflow-auto p-5">
        <Skeleton className="mb-3 h-10 w-72" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          This case couldn&apos;t be loaded — it may have been deleted, or you lack access.
          <button onClick={() => nav("/cases")} className="ml-2 underline">Back to cases</button>
        </div>
      </div>
    );
  }

  const c: CaseFile = q.data.case;
  const timeline: CaseTimelineEvent[] = q.data.timeline;
  const attachments: { label: string; ids: string[] }[] = [
    { label: "Findings", ids: c.finding_uids },
    { label: "Change events", ids: c.change_event_ids },
    { label: "Evidence snapshots", ids: c.evidence_snapshot_ids },
  ].filter((a) => a.ids.length > 0);

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-b bg-white px-5 py-3">
        <button onClick={() => nav("/cases")} className="mb-1 text-[12px] text-gray-500 hover:text-gray-700">
          ← All cases
        </button>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="truncate text-lg font-semibold text-gray-900">{c.title}</h1>
              <StatusChip status={c.status} />
            </div>
            {c.summary && <p className="mt-0.5 max-w-3xl text-[13px] text-gray-500">{c.summary}</p>}
          </div>
          <button
            onClick={async () => {
              if (
                await confirm({
                  title: "Delete case file?",
                  message: "This permanently deletes the case file and its entire timeline. This cannot be undone.",
                  confirmLabel: "Delete",
                  destructive: true,
                })
              )
                del.mutate();
            }}
            disabled={del.isPending}
            className="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-[12px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
          >
            Delete
          </button>
        </div>

        {/* Status transitions */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] font-medium text-gray-500">Move to:</span>
          {STATUS_ORDER.filter((s) => s !== c.status).map((s) => (
            <button
              key={s}
              onClick={() => setStatus.mutate(s)}
              disabled={setStatus.isPending}
              className="rounded-full border px-2.5 py-0.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              {STATUS_META[s].label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        <div className="grid gap-4 lg:grid-cols-3">
          {/* Left: metadata + attachments */}
          <div className="space-y-4">
            <div className="rounded-lg border bg-white p-4">
              <h2 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-gray-400">Details</h2>
              <dl className="space-y-1.5 text-[13px]">
                <Row k="Severity"><span className={SEV_META[c.severity]?.tone}>{SEV_META[c.severity]?.label ?? c.severity}</span></Row>
                <Row k="Workload">{c.workload_name || "—"}</Row>
                <Row k="Assignee">{c.assignee || "Unassigned"}</Row>
                <Row k="Risk score">{c.risk_score ?? "—"}</Row>
                <Row k="Confidence">{c.confidence != null ? `${c.confidence}%` : "—"}</Row>
                <Row k="Opened by">{c.opened_by || "—"}</Row>
                <Row k="Opened">{fmt(c.opened_at)}</Row>
                {c.resolved_at && <Row k="Resolved">{fmt(c.resolved_at)}</Row>}
              </dl>
              {(c.investigation_chat_id || c.architecture_id) && (
                <div className="mt-3 flex flex-wrap gap-2 border-t pt-3">
                  {c.investigation_chat_id && (
                    <button
                      onClick={() => nav(`/c/${c.investigation_chat_id}`)}
                      className="rounded-lg border border-violet-200 bg-violet-50 px-2.5 py-1 text-[12px] font-medium text-violet-700 hover:bg-violet-100"
                    >
                      🔬 Open investigation
                    </button>
                  )}
                  {c.architecture_id && (
                    <button
                      onClick={() => nav(`/architectures`)}
                      className="rounded-lg border px-2.5 py-1 text-[12px] font-medium text-gray-600 hover:bg-gray-50"
                    >
                      📐 Linked architecture
                    </button>
                  )}
                </div>
              )}
            </div>

            {attachments.length > 0 && (
              <div className="rounded-lg border bg-white p-4">
                <h2 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-gray-400">Linked artifacts</h2>
                <div className="space-y-2">
                  {attachments.map((a) => (
                    <div key={a.label}>
                      <div className="text-[12px] font-medium text-gray-700">{a.label} ({a.ids.length})</div>
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {a.ids.slice(0, 12).map((id) => (
                          <code key={id} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600" title={id}>
                            {id.length > 14 ? `${id.slice(0, 14)}…` : id}
                          </code>
                        ))}
                        {a.ids.length > 12 && <span className="text-[10px] text-gray-400">+{a.ids.length - 12} more</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Right: timeline */}
          <div className="lg:col-span-2">
            <div className="rounded-lg border bg-white p-4">
              <h2 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-gray-400">Timeline</h2>

              {/* Add note */}
              <div className="mb-4 flex items-start gap-2">
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && note.trim()) addNote.mutate(); }}
                  rows={2}
                  placeholder="Add a note to the case timeline… (⌘/Ctrl+Enter)"
                  className="min-h-[40px] flex-1 resize-y rounded-lg border px-3 py-1.5 text-sm"
                />
                <button
                  onClick={() => addNote.mutate()}
                  disabled={!note.trim() || addNote.isPending}
                  className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-black disabled:opacity-50"
                >
                  Add
                </button>
              </div>

              <ol className="space-y-3">
                {timeline.map((e) => {
                  const m = EVENT_META[e.kind] ?? { icon: "•", tone: "text-gray-500" };
                  return (
                    <li key={e.id} className="flex gap-2.5">
                      <span className="mt-0.5 text-base leading-none">{m.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className={`text-[13px] ${m.tone}`}>{e.message || e.kind}</div>
                        <div className="text-[11px] text-gray-400">
                          {e.actor || "system"} · <span title={fmt(e.created_at)}>{rel(e.created_at)}</span>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ol>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-[12px] text-gray-400">{k}</dt>
      <dd className="min-w-0 truncate text-right text-gray-800">{children}</dd>
    </div>
  );
}

// ================================================================ entry point
export function CasesPanel() {
  const { id } = useParams();
  return id ? <CaseDetailView caseId={id} /> : <CaseList />;
}
