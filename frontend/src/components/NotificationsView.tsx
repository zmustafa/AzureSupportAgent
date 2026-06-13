import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, type AppNotification, type NotificationRule, type Severity } from "../api";
import { formatError, formatRelativeFromNow, formatTimestamp } from "../utils/format";
import { notificationLink, SEVERITY_DOT } from "../utils/notificationLink";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

const EVENT_TYPES = [
  "task.succeeded",
  "task.failed",
  "workbook.severity",
  "workbook.failed",
  "playbook.completed",
  "investigation.completed",
];
const SOURCES = ["task", "workbook", "playbook", "investigation"];

export function NotificationsSection() {
  const qc = useQueryClient();
  const rulesQ = useQuery({ queryKey: ["notificationRules"], queryFn: api.notificationRules });
  const connQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const [editing, setEditing] = useState<Partial<NotificationRule> | null>(null);
  const [msg, setMsg] = useState("");

  const rules = rulesQ.data?.rules ?? [];
  const connectors = (connQ.data?.connectors ?? []).filter((c) => !c.disabled);

  async function remove(id: string) {
    try {
      await api.deleteNotificationRule(id);
      qc.invalidateQueries({ queryKey: ["notificationRules"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Notifications</h1>
          <p className="mt-1 text-sm text-gray-500">
            Global routing rules. When an event matches a rule, it's delivered to the in-app
            center and/or the selected connectors (Teams, Slack, email, PagerDuty…). With no
            rules, every event still appears in the in-app bell.
          </p>
        </div>
        <button
          onClick={() => setEditing({ name: "", enabled: true, event_types: [], sources: [], min_severity: "warning", in_app: true, connector_ids: [] })}
          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
        >
          + New rule
        </button>
      </div>

      {msg && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{msg}</div>}

      <div className="space-y-2">
        {rules.map((r) => (
          <div key={r.id} className="flex items-center gap-3 rounded-xl border bg-white p-4 shadow-sm">
            <div className={`h-2 w-2 shrink-0 rounded-full ${r.enabled ? "bg-green-500" : "bg-gray-300"}`} />
            <div className="min-w-0 flex-1">
              <div className="font-medium text-gray-800">{r.name || "(unnamed rule)"}</div>
              <div className="mt-0.5 text-xs text-gray-500">
                {r.event_types.length ? r.event_types.join(", ") : "any event"} · severity ≥ {r.min_severity}
                {" → "}
                {[r.in_app ? "in-app" : null, ...r.connector_ids.map((id) => connectors.find((c) => c.id === id)?.name ?? id)].filter(Boolean).join(", ") || "no targets"}
              </div>
            </div>
            <button onClick={() => setEditing(r)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Edit</button>
            <button onClick={() => void remove(r.id)} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
          </div>
        ))}
        {rules.length === 0 && (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
            No rules yet — all events go to the in-app center by default. Add a rule to also
            route events to connectors.
          </div>
        )}
      </div>

      {editing && (
        <RuleForm
          value={editing}
          connectors={connectors.map((c) => ({ id: c.id, name: c.name }))}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["notificationRules"] });
          }}
        />
      )}
    </div>
  );
}

function RuleForm({
  value,
  connectors,
  onClose,
  onSaved,
}: {
  value: Partial<NotificationRule>;
  connectors: { id: string; name: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Partial<NotificationRule>>(value);
  const [error, setError] = useState("");
  const set = (patch: Partial<NotificationRule>) => setForm((f) => ({ ...f, ...patch }));

  function toggle(list: string[], v: string): string[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }

  async function save() {
    if (!form.name?.trim()) {
      setError("Name the rule.");
      return;
    }
    try {
      await api.upsertNotificationRule(form);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="flex max-h-[92vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">{form.id ? "Edit rule" : "New rule"}</h2>
          <button onClick={onClose} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
          <div>
            <label className={label}>Rule name</label>
            <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input type="checkbox" checked={form.enabled ?? true} onChange={(e) => set({ enabled: e.target.checked })} />
            Enabled
          </label>

          <div>
            <label className={label}>Match event types (none = any)</label>
            <div className="flex flex-wrap gap-1.5">
              {EVENT_TYPES.map((t) => {
                const on = (form.event_types ?? []).includes(t);
                return (
                  <button key={t} onClick={() => set({ event_types: toggle(form.event_types ?? [], t) })}
                    className={`rounded-lg border px-2 py-1 text-[11px] ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>
                    {on ? "✓ " : ""}{t}
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <label className={label}>Match sources (none = any)</label>
            <div className="flex flex-wrap gap-1.5">
              {SOURCES.map((t) => {
                const on = (form.sources ?? []).includes(t);
                return (
                  <button key={t} onClick={() => set({ sources: toggle(form.sources ?? [], t) })}
                    className={`rounded-lg border px-2 py-1 text-[11px] ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>
                    {on ? "✓ " : ""}{t}
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <label className={label}>Minimum severity</label>
            <select className={input} value={form.min_severity ?? "warning"} onChange={(e) => set({ min_severity: e.target.value as Severity })}>
              {["info", "warning", "error", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          <div className="rounded-lg border p-3">
            <div className="mb-2 text-xs font-medium text-gray-600">Deliver to</div>
            <label className="flex items-center gap-2 text-sm text-gray-700">
              <input type="checkbox" checked={form.in_app ?? true} onChange={(e) => set({ in_app: e.target.checked })} />
              In-app center (bell)
            </label>
            <div className="mt-2 space-y-1">
              {connectors.length === 0 && <p className="text-[11px] text-gray-400">No connectors configured.</p>}
              {connectors.map((c) => {
                const on = (form.connector_ids ?? []).includes(c.id);
                return (
                  <label key={c.id} className="flex items-center gap-2 text-sm text-gray-700">
                    <input type="checkbox" checked={on} onChange={() => set({ connector_ids: toggle(form.connector_ids ?? [], c.id) })} />
                    {c.name}
                  </label>
                );
              })}
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
          {error && <div className="mr-auto text-xs text-red-600">{error}</div>}
          <button onClick={onClose} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// Full notifications page (all events, clickable to their source)
// ===========================================================================
const NOTE_SEV_BADGE: Record<string, string> = {
  info: "bg-gray-100 text-gray-600",
  warning: "bg-amber-100 text-amber-700",
  error: "bg-orange-100 text-orange-700",
  critical: "bg-red-100 text-red-700",
};
const NOTE_SOURCE_LABEL: Record<string, string> = {
  assessment: "Assessment",
  workbook: "Workbook",
  playbook: "Playbook",
  task: "Scheduled task",
};

export function NotificationsPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"all" | "unread">("all");
  const [src, setSrc] = useState("all");
  const [search, setSearch] = useState("");

  const q = useQuery({
    queryKey: ["notificationsList"],
    queryFn: () => api.notifications(false),
    refetchInterval: 20_000,
  });
  const all = q.data?.notifications ?? [];
  const sources = useMemo(() => Array.from(new Set(all.map((n) => n.source))).sort(), [all]);

  const term = search.trim().toLowerCase();
  const shown = all.filter((n) => {
    if (tab === "unread" && n.read) return false;
    if (src !== "all" && n.source !== src) return false;
    if (term && !`${n.title} ${n.body}`.toLowerCase().includes(term)) return false;
    return true;
  });
  const unreadCount = all.filter((n) => !n.read).length;

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["notificationsList"] });
    qc.invalidateQueries({ queryKey: ["notificationsUnread"] });
  }
  async function markRead(id: string) { await api.markNotificationRead(id); invalidate(); }
  async function markAll() { await api.markAllNotificationsRead(); invalidate(); }
  function open(n: AppNotification) {
    const to = notificationLink(n);
    if (!n.read) void markRead(n.id);
    if (to) navigate(to);
  }

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-800">Notifications</h1>
            <p className="mt-1 text-sm text-gray-500">
              Every event the workspace has surfaced — assessments, workbooks, playbooks, and
              scheduled tasks. Click one to open the run or report it came from.
            </p>
          </div>
          <Link to="/automations/notifications" className="shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">
            ⚙ Manage rules
          </Link>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-md border text-xs">
            <button onClick={() => setTab("all")} className={`px-2.5 py-1 ${tab === "all" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All ({all.length})</button>
            <button onClick={() => setTab("unread")} className={`border-l px-2.5 py-1 ${tab === "unread" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>Unread ({unreadCount})</button>
          </div>
          {sources.length > 1 && (
            <select value={src} onChange={(e) => setSrc(e.target.value)} className="rounded-md border px-2 py-1 text-xs text-gray-600 focus:outline-none focus:ring-1 focus:ring-brand">
              <option value="all">All sources</option>
              {sources.map((s) => <option key={s} value={s}>{NOTE_SOURCE_LABEL[s] ?? s}</option>)}
            </select>
          )}
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search…" className="w-44 rounded-md border px-2.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-brand" />
          {unreadCount > 0 && (
            <button onClick={() => void markAll()} className="ml-auto rounded-md border px-2.5 py-1 text-xs text-brand hover:bg-brand/5">Mark all read</button>
          )}
        </div>

        {q.isLoading && <div className="text-sm text-gray-400">Loading…</div>}
        {!q.isLoading && shown.length === 0 && (
          <div className="rounded-xl border border-dashed bg-white px-6 py-12 text-center text-sm text-gray-500">
            {all.length === 0 ? "No notifications yet." : "No notifications match these filters."}
          </div>
        )}

        <div className="space-y-2">
          {shown.map((n) => {
            const to = notificationLink(n);
            return (
              <div
                key={n.id}
                onClick={() => open(n)}
                role={to ? "button" : undefined}
                tabIndex={to ? 0 : undefined}
                onKeyDown={to ? (e) => { if (e.key === "Enter") open(n); } : undefined}
                className={`flex gap-3 rounded-xl border bg-white p-3 shadow-sm transition ${to ? "cursor-pointer hover:border-brand/40 hover:shadow-md" : ""} ${n.read ? "" : "border-brand/30 bg-brand/5"}`}
              >
                <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${SEVERITY_DOT[n.severity] ?? "bg-gray-400"}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-gray-800">{n.title}</span>
                    {!n.read && <span className="shrink-0 rounded-full bg-brand px-1.5 py-0.5 text-[9px] font-medium text-white">new</span>}
                    <span className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${NOTE_SEV_BADGE[n.severity] ?? NOTE_SEV_BADGE.info}`}>{n.severity}</span>
                  </div>
                  {n.body && <p className="mt-0.5 whitespace-pre-wrap text-xs text-gray-600">{n.body}</p>}
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">{NOTE_SOURCE_LABEL[n.source] ?? n.source}</span>
                    {n.created_at && <span title={formatTimestamp(n.created_at)}>{formatRelativeFromNow(n.created_at)}</span>}
                    {to && <span className="text-brand">· Open →</span>}
                    {!n.read && (
                      <button onClick={(e) => { e.stopPropagation(); void markRead(n.id); }} className="ml-auto text-brand hover:underline">Mark read</button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
