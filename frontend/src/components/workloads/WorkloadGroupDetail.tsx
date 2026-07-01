// Workload Group command-center — the detail page for one application / service family
// (e.g. "CRM" containing the separate "CRM PROD" and "CRM DEV" workloads). Shows a rolled-up
// health/risk/composition summary across its members, the member list (open / mission / remove),
// a one-click "launch missions for every member" action, and edit / delete for the group itself.
//
// Route: /workloads/groups/:id  (rendered by ChatView). The group is a non-destructive
// association — removing a member or deleting the group never touches the underlying workloads.
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type WorkloadGroupBase,
  type WorkloadGroupRollup,
  type WorkloadGroupCompare,
  type WorkloadGroupCompareMember,
  type WorkloadGroupCompareSignal,
  type WorkloadProfile,
} from "../../api";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";

type Tone = "gray" | "green" | "amber" | "red" | "indigo";

const TONE_CLS: Record<Tone, string> = {
  gray: "bg-gray-100 text-gray-600",
  green: "bg-green-50 text-green-700",
  amber: "bg-amber-50 text-amber-700",
  red: "bg-red-50 text-red-700",
  indigo: "bg-indigo-50 text-indigo-700",
};

function Chip({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${TONE_CLS[tone]}`}>{children}</span>;
}

const bandTone = (band?: string): Tone =>
  band === "good" ? "green" : band === "warn" ? "amber" : band === "poor" ? "red" : "gray";
const critTone = (c?: string): Tone => (c === "critical" || c === "high" ? "red" : c ? "gray" : "gray");

function Kpi({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-xl border bg-white px-3 py-2">
      <div className={`text-lg font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
    </div>
  );
}

// Rollup summary chips (kept local so this route's lazy chunk stays independent of WorkloadsView).
function RollupChips({ rollup }: { rollup: WorkloadGroupRollup }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Chip tone={bandTone(rollup.health.band)}>
        ♥ {rollup.health.avg_score ?? "—"}
        {rollup.analyzed_count < rollup.member_count ? ` · ${rollup.analyzed_count}/${rollup.member_count} analyzed` : ""}
      </Chip>
      <Chip tone="gray">▦ {rollup.total_resources} resources</Chip>
      {rollup.criticality && <Chip tone={critTone(rollup.criticality)}>◆ {rollup.criticality}</Chip>}
      {rollup.risk.retirements_90d > 0 && <Chip tone="amber">⚠ {rollup.risk.retirements_90d} retiring ≤90d</Chip>}
      {rollup.risk.criticals > 0 && <Chip tone="red">🔴 {rollup.risk.criticals} critical</Chip>}
    </div>
  );
}

// Inline modal to edit the group's own metadata (name / description / owner / colour).
function GroupEditModal({
  group,
  onClose,
  onSaved,
}: {
  group: WorkloadGroupBase;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(group.name);
  const [description, setDescription] = useState(group.description ?? "");
  const [owner, setOwner] = useState(group.owner ?? "");
  const [color, setColor] = useState(group.color || "#6366f1");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    if (!name.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await api.upsertWorkloadGroup({ id: group.id, name: name.trim(), description: description.trim(), owner: owner.trim(), color });
      onSaved();
    } catch (e) {
      setErr(formatError(e));
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-gray-800">Edit group</h2>
        <div className="mt-4 space-y-3">
          <label className="block">
            <span className="text-xs font-medium text-gray-600">Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} className="mt-1 w-full rounded-lg border px-2 py-1.5 text-sm" />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-gray-600">Description</span>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} className="mt-1 w-full rounded-lg border px-2 py-1.5 text-sm" />
          </label>
          <div className="flex gap-3">
            <label className="flex-1">
              <span className="text-xs font-medium text-gray-600">Owner</span>
              <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="team / person" className="mt-1 w-full rounded-lg border px-2 py-1.5 text-sm" />
            </label>
            <label>
              <span className="text-xs font-medium text-gray-600">Colour</span>
              <input type="color" value={color} onChange={(e) => setColor(e.target.value)} className="mt-1 h-9 w-12 cursor-pointer rounded-lg border" />
            </label>
          </div>
        </div>
        {err && <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">{err}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button disabled={busy || !name.trim()} onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---- Compare (PROD-vs-DEV drift) ------------------------------------------------
const envLabelOf = (m: WorkloadGroupCompareMember): string => m.environment || m.name || "?";

// Count-based drift matrix (resource types / categories). "missing" = 0 in this member.
function DriftMatrix({
  title,
  members,
  rows,
  emptyLabel,
}: {
  title: string;
  members: WorkloadGroupCompareMember[];
  rows: { key: string; label: string; counts: Record<string, number>; drift: boolean }[];
  emptyLabel: string;
}) {
  return (
    <div className="overflow-hidden rounded-xl border bg-white">
      <div className="border-b px-4 py-2.5"><h3 className="text-sm font-semibold text-gray-700">{title}</h3></div>
      {rows.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-gray-400">{emptyLabel}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b bg-gray-50 text-[11px] uppercase tracking-wide text-gray-400">
              <tr>
                <th className="px-4 py-2 text-left font-medium">{title}</th>
                {members.map((m) => (
                  <th key={m.id} className="px-3 py-2 text-center font-medium text-gray-700">{envLabelOf(m)}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((r) => (
                <tr key={r.key} className={r.drift ? "bg-amber-50/40" : ""}>
                  <td className="px-4 py-2 text-gray-700">{r.label}</td>
                  {members.map((m) => {
                    const c = r.counts[m.id] ?? 0;
                    return (
                      <td key={m.id} className="px-3 py-2 text-center tabular-nums">
                        {c > 0 ? (
                          <span className="text-gray-800">{c}</span>
                        ) : (
                          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-500">missing</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// Score-based coverage matrix (health signals). null value = not covered in this member.
function SignalMatrix({
  members,
  signals,
}: {
  members: WorkloadGroupCompareMember[];
  signals: WorkloadGroupCompareSignal[];
}) {
  return (
    <div className="overflow-hidden rounded-xl border bg-white">
      <div className="border-b px-4 py-2.5"><h3 className="text-sm font-semibold text-gray-700">Health-signal coverage</h3></div>
      {signals.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-gray-400">Signal coverage is consistent across members.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b bg-gray-50 text-[11px] uppercase tracking-wide text-gray-400">
              <tr>
                <th className="px-4 py-2 text-left font-medium">Signal</th>
                {members.map((m) => (
                  <th key={m.id} className="px-3 py-2 text-center font-medium text-gray-700">{envLabelOf(m)}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {signals.map((s) => (
                <tr key={s.key} className={s.drift ? "bg-amber-50/40" : ""}>
                  <td className="px-4 py-2 text-gray-700">{s.label}</td>
                  {members.map((m) => {
                    const v = s.values[m.id];
                    return (
                      <td key={m.id} className="px-3 py-2 text-center tabular-nums">
                        {typeof v === "number" ? (
                          <span className="text-gray-800">{v}</span>
                        ) : (
                          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-500">none</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// The full "compare environments" view: aligned member summary + resource-type / category /
// signal drift matrices + human-readable highlights. Members render as columns for side-by-side.
function CompareView({ compare }: { compare: WorkloadGroupCompare }) {
  const { members, signals, categories, types, highlights, summary } = compare;
  const [driftOnly, setDriftOnly] = useState(true);

  if (members.length < 2) {
    return (
      <div className="rounded-xl border border-dashed bg-white p-10 text-center">
        <div className="text-3xl">⚖️</div>
        <p className="mt-2 text-sm font-medium text-gray-700">Need at least two members to compare.</p>
        <p className="mt-1 text-xs text-gray-500">Add another environment (e.g. a DEV workload) to this group to see PROD-vs-DEV drift.</p>
      </div>
    );
  }

  const shownTypes = (driftOnly ? types.filter((t) => t.drift) : types).map((t) => ({
    key: t.type,
    label: t.friendly,
    counts: t.counts,
    drift: t.drift,
  }));
  const shownCats = (driftOnly ? categories.filter((c) => c.drift) : categories).map((c) => ({
    key: c.category,
    label: c.category,
    counts: c.counts,
    drift: c.drift,
  }));
  const shownSignals = driftOnly ? signals.filter((s) => s.drift) : signals;

  return (
    <div className="space-y-4">
      {/* Summary chips */}
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={summary.drift_types ? "amber" : "green"}>◲ {summary.drift_types} resource-type drift</Chip>
        <Chip tone={summary.drift_signals ? "amber" : "green"}>♥ {summary.drift_signals} signal-coverage drift</Chip>
        <Chip tone={summary.drift_categories ? "amber" : "gray"}>▦ {summary.drift_categories} category drift</Chip>
        {summary.health_spread > 0 && (
          <Chip tone={summary.health_spread >= 20 ? "red" : "gray"}>Δ {summary.health_spread} health spread</Chip>
        )}
      </div>

      {/* Highlights */}
      {highlights.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-amber-700">Drift highlights</h3>
          <ul className="space-y-1 text-sm text-amber-900">
            {highlights.map((h, i) => (
              <li key={i} className="flex gap-2"><span>⚠</span><span>{h}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* Member overview (members as columns) */}
      <div className="overflow-x-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="border-b bg-gray-50 text-[11px] uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-4 py-2 text-left font-medium">Attribute</th>
              {members.map((m) => (
                <th key={m.id} className="px-3 py-2 text-center font-medium">
                  <div className="text-gray-700">{envLabelOf(m)}</div>
                  {m.environment && m.name !== m.environment && (
                    <div className="text-[10px] font-normal normal-case text-gray-400">{m.name}</div>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            <tr>
              <td className="px-4 py-2 text-gray-500">Criticality</td>
              {members.map((m) => (
                <td key={m.id} className="px-3 py-2 text-center">
                  {m.criticality ? <Chip tone={critTone(m.criticality)}>{m.criticality}</Chip> : <span className="text-gray-300">—</span>}
                </td>
              ))}
            </tr>
            <tr>
              <td className="px-4 py-2 text-gray-500">Data class</td>
              {members.map((m) => (
                <td key={m.id} className="px-3 py-2 text-center text-gray-600">{m.data_classification || "—"}</td>
              ))}
            </tr>
            <tr>
              <td className="px-4 py-2 text-gray-500">Resources</td>
              {members.map((m) => (
                <td key={m.id} className="px-3 py-2 text-center tabular-nums text-gray-700">{m.total_resources}</td>
              ))}
            </tr>
            <tr>
              <td className="px-4 py-2 text-gray-500">Health</td>
              {members.map((m) => (
                <td key={m.id} className="px-3 py-2 text-center">
                  {m.health_score != null ? <Chip tone={bandTone(m.health_band)}>♥ {m.health_score}</Chip> : <span className="text-gray-300">n/a</span>}
                </td>
              ))}
            </tr>
            <tr>
              <td className="px-4 py-2 text-gray-500">Retiring ≤90d</td>
              {members.map((m) => (
                <td key={m.id} className={`px-3 py-2 text-center tabular-nums ${m.retirements_90d ? "text-amber-600" : "text-gray-400"}`}>{m.retirements_90d}</td>
              ))}
            </tr>
            <tr>
              <td className="px-4 py-2 text-gray-500">Critical signals</td>
              {members.map((m) => (
                <td key={m.id} className={`px-3 py-2 text-center tabular-nums ${m.criticals ? "text-red-600" : "text-gray-400"}`}>{m.criticals}</td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>

      <label className="flex items-center gap-2 text-xs text-gray-500">
        <input type="checkbox" checked={driftOnly} onChange={(e) => setDriftOnly(e.target.checked)} />
        Show only rows that differ between members
      </label>

      <DriftMatrix title="Resource types" members={members} rows={shownTypes} emptyLabel="No resource-type differences between members." />
      <DriftMatrix title="Resource categories" members={members} rows={shownCats} emptyLabel="No category differences between members." />
      <SignalMatrix members={members} signals={shownSignals} />
    </div>
  );
}

export function WorkloadGroupDetailPanel() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"members" | "compare">("members");

  const q = useQuery({
    queryKey: ["workloadGroup", id],
    queryFn: () => api.workloadGroup(id),
    enabled: !!id,
    retry: false,
  });
  const detail = q.data;
  const group = detail?.group;
  const members = detail?.members ?? [];
  const rollup = detail?.rollup;

  // Lazy: only fetch the PROD-vs-DEV drift comparison when the Compare tab is opened.
  const compareQ = useQuery({
    queryKey: ["workloadGroupCompare", id],
    queryFn: () => api.workloadGroupCompare(id),
    enabled: !!id && tab === "compare",
    retry: false,
  });

  const profileById = useMemo(() => {
    const m = new Map<string, WorkloadProfile>();
    for (const p of detail?.profiles ?? []) m.set(p.id, p);
    return m;
  }, [detail?.profiles]);

  async function refetchAll() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["workloadGroup", id] }),
      qc.invalidateQueries({ queryKey: ["workloads"] }),
      qc.invalidateQueries({ queryKey: ["workloadGroups"] }),
      qc.invalidateQueries({ queryKey: ["workloadGroupSuggest"] }),
    ]);
  }

  async function launchAll() {
    if (members.length === 0) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.runFleet({ workload_ids: members.map((m) => m.id) });
      setMsg({ text: `🚀 Launched ${r.launched} mission${r.launched === 1 ? "" : "s"}. Open a member's Mission Control to watch progress.`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function assessAll() {
    if (members.length === 0) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.enqueueAssessments({ workload_ids: members.map((m) => m.id), pack: "waf" });
      setMsg({ text: `🛡 Queued ${r.queued} WAF assessment${r.queued === 1 ? "" : "s"}. Track progress on the Assessments page.`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function architectAll() {
    if (members.length === 0) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.createArchitectureJobs(members.map((m) => m.id));
      setMsg({ text: `🏛 Queued ${r.queued} architecture build${r.queued === 1 ? "" : "s"}. Track progress on the Architectures page.`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function removeMember(wid: string, name: string) {
    if (!window.confirm(`Remove “${name}” from this group? The workload itself is not affected.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      await api.assignWorkloadGroup({ group_id: id, workload_ids: [wid], mode: "remove" });
      await refetchAll();
      setMsg({ text: `Removed “${name}” from the group.`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function del() {
    if (!group) return;
    if (!window.confirm(`Delete group “${group.name}”? Its ${members.length} workload${members.length === 1 ? "" : "s"} stay — they just lose this group association.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      await api.deleteWorkloadGroup(id);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["workloads"] }),
        qc.invalidateQueries({ queryKey: ["workloadGroups"] }),
        qc.invalidateQueries({ queryKey: ["workloadGroupSuggest"] }),
      ]);
      navigate("/workloads");
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="border-b bg-white px-6 py-4">
        <button onClick={() => navigate("/workloads")} className="text-xs text-gray-400 hover:text-gray-600">← Workloads</button>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">
              <span className="inline-block h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: group?.color || "#6366f1" }} />
              <span className="truncate">{group?.name ?? "Group"}</span>
            </h1>
            {group?.description && <p className="mt-0.5 max-w-2xl text-sm text-gray-500">{group.description}</p>}
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-400">
              {group?.owner && <span>👤 {group.owner}</span>}
              <span>{members.length} member{members.length === 1 ? "" : "s"}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => void launchAll()}
              disabled={busy || members.length === 0}
              className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-50"
              title="Launch the full mission suite for every workload in this group"
            >
              🚀 Launch missions
            </button>
            <button
              onClick={() => void assessAll()}
              disabled={busy || members.length === 0}
              className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              title="Queue a Well-Architected assessment for every workload in this group"
            >
              🛡 Assess all
            </button>
            <button
              onClick={() => void architectAll()}
              disabled={busy || members.length === 0}
              className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              title="Build an architecture diagram for every workload in this group"
            >
              🏛 Build architectures
            </button>
            <button onClick={() => setEditing(true)} disabled={!group} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">✎ Edit</button>
            <button onClick={() => void del()} disabled={busy || !group} className="rounded-lg border border-red-200 px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50">🗑 Delete</button>
          </div>
        </div>

        {rollup && (
          <>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Kpi label="Members" value={rollup.member_count} />
              <Kpi label="Resources" value={rollup.total_resources} />
              <Kpi
                label="Avg health"
                value={rollup.health.avg_score ?? "—"}
                tone={rollup.health.band === "good" ? "text-green-600" : rollup.health.band === "warn" ? "text-amber-600" : rollup.health.band === "poor" ? "text-red-600" : undefined}
              />
              <Kpi label="Critical signals" value={rollup.risk.criticals} tone={rollup.risk.criticals ? "text-red-600" : "text-green-600"} />
            </div>
            <div className="mt-2"><RollupChips rollup={rollup} /></div>
          </>
        )}
        {msg && (
          <div className={`mt-2 rounded-lg border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}
      </header>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto bg-gray-50 p-6">
        {q.isLoading ? (
          <Skeleton rows={6} />
        ) : q.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(q.error)}</div>
        ) : !group ? (
          <div className="rounded-xl border border-dashed bg-white p-10 text-center">
            <div className="text-3xl">🔍</div>
            <p className="mt-2 text-sm font-medium text-gray-700">Group not found.</p>
            <button onClick={() => navigate("/workloads")} className="mt-3 rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">← Back to Workloads</button>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Tab bar */}
            <div className="flex w-fit items-center gap-0.5 rounded-lg border bg-gray-50 p-0.5 text-xs">
              <button
                onClick={() => setTab("members")}
                className={tab === "members" ? "rounded-md bg-white px-3 py-1 font-medium text-gray-800 shadow-sm" : "rounded-md px-3 py-1 text-gray-500 hover:text-gray-700"}
              >
                Members
              </button>
              <button
                onClick={() => setTab("compare")}
                className={tab === "compare" ? "rounded-md bg-white px-3 py-1 font-medium text-gray-800 shadow-sm" : "rounded-md px-3 py-1 text-gray-500 hover:text-gray-700"}
              >
                ⚖️ Compare environments
              </button>
            </div>

            {tab === "members" ? (
              <>
            {/* Members */}
            <div className="overflow-hidden rounded-xl border bg-white">
              <div className="flex items-center justify-between border-b px-4 py-2.5">
                <h2 className="text-sm font-semibold text-gray-700">Members</h2>
                <span className="text-xs text-gray-400">Each workload keeps its own identity, missions and assessments.</span>
              </div>
              {members.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-gray-500">
                  No members. Add workloads to this group from the Workloads list (select → ⊞ Group).
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead className="border-b bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-400">
                    <tr>
                      <th className="px-4 py-2 font-medium">Workload</th>
                      <th className="px-3 py-2 font-medium">Environment</th>
                      <th className="px-3 py-2 font-medium">Criticality</th>
                      <th className="px-3 py-2 font-medium">Health</th>
                      <th className="px-3 py-2 text-right font-medium">Resources</th>
                      <th className="px-3 py-2 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {members.map((w) => {
                      const p = profileById.get(w.id);
                      const score = p?.health?.score;
                      const band = p?.health?.band;
                      const total = p?.composition?.total;
                      return (
                        <tr key={w.id} className="hover:bg-gray-50">
                          <td className="px-4 py-2">
                            <button onClick={() => navigate(`/workloads/${w.id}`)} className="font-medium text-gray-800 hover:text-brand hover:underline">{w.name}</button>
                          </td>
                          <td className="px-3 py-2 text-gray-500">{w.environment || "—"}</td>
                          <td className="px-3 py-2">{w.criticality ? <Chip tone={critTone(w.criticality)}>{w.criticality}</Chip> : <span className="text-gray-400">—</span>}</td>
                          <td className="px-3 py-2">{score != null ? <Chip tone={bandTone(band)}>♥ {score}</Chip> : <span className="text-gray-400">not analyzed</span>}</td>
                          <td className="px-3 py-2 text-right tabular-nums text-gray-600">{total ?? "—"}</td>
                          <td className="px-3 py-2">
                            <div className="flex items-center justify-end gap-1.5">
                              <button onClick={() => navigate(`/mission-control/${w.id}`)} className="rounded-md border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50" title="Mission Control">🚀</button>
                              <button onClick={() => navigate(`/workloads/${w.id}`)} className="rounded-md border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50" title="Open workload">Open</button>
                              <button onClick={() => void removeMember(w.id, w.name)} disabled={busy} className="rounded-md border border-red-200 px-2 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50" title="Remove from group">Remove</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>

            {/* Breakdowns */}
            {rollup && (rollup.by_category.length > 0 || rollup.by_environment.length > 0) && (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {rollup.by_environment.length > 0 && (
                  <div className="rounded-xl border bg-white p-4">
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">By environment</h3>
                    <div className="flex flex-wrap gap-1.5">
                      {rollup.by_environment.map((e) => (
                        <Chip key={e.environment} tone="indigo">{e.environment}: {e.count}</Chip>
                      ))}
                    </div>
                  </div>
                )}
                {rollup.by_category.length > 0 && (
                  <div className="rounded-xl border bg-white p-4">
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Resource categories</h3>
                    <div className="flex flex-wrap gap-1.5">
                      {rollup.by_category.slice(0, 12).map((c) => (
                        <Chip key={c.category} tone="gray">{c.category}: {c.count}</Chip>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
              </>
            ) : compareQ.isLoading ? (
              <Skeleton rows={6} />
            ) : compareQ.isError ? (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(compareQ.error)}</div>
            ) : compareQ.data ? (
              <CompareView compare={compareQ.data.compare} />
            ) : null}
          </div>
        )}
      </div>

      {editing && group && (
        <GroupEditModal
          group={group}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            void refetchAll();
            setMsg({ text: "Group updated.", ok: true });
          }}
        />
      )}
    </div>
  );
}
