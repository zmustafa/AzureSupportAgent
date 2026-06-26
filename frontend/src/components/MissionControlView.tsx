import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  streamMission,
  type Mission,
  type MissionLog,
  type MissionSystem,
} from "../api";
import { formatError } from "../utils/format";

/** A small spinning ring used wherever something is actively running. */
function Spinner({ className = "h-3 w-3" }: { className?: string }) {
  return <span className={`inline-block animate-spin rounded-full border-2 border-current border-t-transparent ${className}`} aria-hidden />;
}

// Map a per-system status to a compact chip.
function StatusChip({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string; spin?: boolean }> = {
    idle: { label: "not run", cls: "bg-gray-100 text-gray-500" },
    queued: { label: "queued", cls: "bg-gray-100 text-gray-600" },
    running: { label: "running", cls: "bg-blue-100 text-blue-700", spin: true },
    done: { label: "done", cls: "bg-green-100 text-green-700" },
    skipped: { label: "fresh", cls: "bg-violet-100 text-violet-700" },
    fail: { label: "failed", cls: "bg-red-100 text-red-700" },
    error: { label: "error", cls: "bg-red-100 text-red-700" },
  };
  const m = map[status] ?? map.idle;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${m.cls}`}>
      {m.spin && <span className="inline-block h-2.5 w-2.5 animate-spin rounded-full border border-current border-t-transparent" />}
      {m.label}
    </span>
  );
}

// Readiness rollup computed from the current board.
function rollup(systems: MissionSystem[]): { readiness: string; done: number; total: number; attention: number } {
  const total = systems.length;
  const done = systems.filter((s) => s.status === "done" || s.status === "skipped").length;
  const attention = systems.filter((s) => s.attention || s.status === "fail" || s.status === "error").length;
  const hardFail = systems.some((s) => s.status === "fail" || s.status === "error");
  const anyRun = systems.some((s) => s.status !== "idle" && s.status !== "queued");
  let readiness = "unknown";
  if (anyRun) {
    if (hardFail) readiness = "nogo";
    else if (attention > 0) readiness = "warn";
    else if (done === total && total > 0) readiness = "go";
    else readiness = "unknown";
  }
  return { readiness, done, total, attention };
}

const READINESS_META: Record<string, { label: string; ring: string; text: string }> = {
  go: { label: "All systems go", ring: "#16a34a", text: "text-green-700" },
  warn: { label: "Go with warnings", ring: "#d97706", text: "text-amber-700" },
  nogo: { label: "No-go", ring: "#dc2626", text: "text-red-700" },
  unknown: { label: "Not assessed", ring: "#9ca3af", text: "text-gray-500" },
};

function ReadinessRing({ systems }: { systems: MissionSystem[] }) {
  const { readiness, done, total, attention } = rollup(systems);
  const meta = READINESS_META[readiness] ?? READINESS_META.unknown;
  const frac = total > 0 ? done / total : 0;
  const r = 26;
  const c = 2 * Math.PI * r;
  return (
    <div className="flex items-center gap-3">
      <svg width="64" height="64" viewBox="0 0 64 64" className="shrink-0">
        <circle cx="32" cy="32" r={r} fill="none" stroke="#e5e7eb" strokeWidth="6" />
        <circle
          cx="32" cy="32" r={r} fill="none" stroke={meta.ring} strokeWidth="6" strokeLinecap="round"
          strokeDasharray={`${frac * c} ${c}`} transform="rotate(-90 32 32)"
        />
        <text x="32" y="36" textAnchor="middle" className="fill-gray-700 text-[13px] font-semibold">{done}/{total}</text>
      </svg>
      <div>
        <div className={`text-sm font-semibold ${meta.text}`}>{meta.label}</div>
        <div className="text-xs text-gray-500">{attention} need{attention === 1 ? "s" : ""} attention</div>
      </div>
    </div>
  );
}

function ageLabel(seconds?: number | null): string {
  if (seconds == null) return "";
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function SystemTile({ system, onRun, busy }: { system: MissionSystem; onRun: (key: string) => void; busy: boolean }) {
  const navigate = useNavigate();
  const open = (link: string) => {
    if (!link) return;
    navigate(link);
  };
  const attention = system.attention || system.status === "fail" || system.status === "error";
  return (
    <div className={`flex items-center gap-3 rounded-xl border bg-white p-3 ${attention ? "border-amber-200" : ""}`}>
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gray-50 text-lg">{system.icon}</div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-800">{system.label}</span>
          <StatusChip status={system.status} />
          {system.fresh && system.status !== "running" && (
            <span className="text-[10px] text-gray-400">{ageLabel(system.age_seconds)}</span>
          )}
        </div>
        <div className="truncate text-xs text-gray-500" title={system.detail || system.headline}>
          {system.headline || (system.status === "idle" ? "Not run yet" : "")}
          {system.error ? <span className="text-red-600"> · {system.error}</span> : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <button
          onClick={() => onRun(system.key)}
          disabled={busy || system.status === "running" || system.status === "queued"}
          className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          title="Run just this system"
        >
          {system.status === "running"
            ? <><Spinner className="h-3 w-3 text-blue-600" /> Running…</>
            : system.status === "queued"
            ? "Queued…"
            : "Run"}
        </button>
        <button
          onClick={() => open(system.link)}
          disabled={!system.link}
          className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-40"
        >
          Open →
        </button>
      </div>
    </div>
  );
}

function MissionBoard({ workloadId }: { workloadId: string }) {
  const navigate = useNavigate();
  const [systems, setSystems] = useState<MissionSystem[]>([]);
  const [log, setLog] = useState<MissionLog[]>([]);
  // Auto-scroll the mission log to the bottom as new lines stream in.
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log.length]);
  const [active, setActive] = useState<Mission | null>(null);
  const [running, setRunning] = useState(false);
  const [force, setForce] = useState(false);
  const [err, setErr] = useState("");
  const [confirmDelete, setConfirmDelete] = useState("");   // mission id pending delete confirm
  const abortRef = useRef<AbortController | null>(null);

  // Connection display name for the header pill.
  const connQ = useQuery({ queryKey: ["azure-connections"], queryFn: api.azureConnections });
  // Initial board from cached last-runs (never scans Azure).
  const stateQ = useQuery({
    queryKey: ["mission-state", workloadId],
    queryFn: () => api.missionState(workloadId),
    enabled: !!workloadId,
  });
  // History.
  const historyQ = useQuery({
    queryKey: ["missions", workloadId],
    queryFn: () => api.listMissions(workloadId, 20),
    enabled: !!workloadId,
  });

  useEffect(() => {
    if (stateQ.data?.systems && !active) setSystems(stateQ.data.systems);
  }, [stateQ.data, active]);

  const connName = useMemo(() => {
    const cid = stateQ.data?.connection_id || active?.connection_id || "";
    const c = (connQ.data?.connections ?? []).find((x) => x.id === cid);
    return c?.display_name || "";
  }, [connQ.data, stateQ.data, active]);

  // Merge an incoming system delta into the board (update in place by key; never drop the
  // other tiles — a single-system run must not collapse the full 8-tile board).
  const applySystem = useCallback((s: MissionSystem) => {
    setSystems((prev) => {
      const idx = prev.findIndex((x) => x.key === s.key);
      if (idx === -1) return [...prev, s];
      const next = prev.slice();
      // A live run clears the cached freshness chip so a just-run tile never shows a stale
      // "Nm ago" age next to a fresh result.
      const clearAge = s.status === "running" ? { fresh: false, age_seconds: null } : {};
      next[idx] = { ...next[idx], ...s, ...clearAge };
      return next;
    });
  }, []);

  // Merge a mission's (possibly partial) systems into the full board, preserving every tile
  // that the mission didn't touch (e.g. a single-system run keeps the other 7 as-is).
  const mergeMissionSystems = useCallback((incoming: MissionSystem[]) => {
    if (!incoming?.length) return;
    setSystems((prev) => {
      if (!prev.length) return incoming;
      const next = prev.slice();
      for (const s of incoming) {
        const idx = next.findIndex((x) => x.key === s.key);
        if (idx === -1) next.push(s);
        else next[idx] = { ...next[idx], ...s };
      }
      return next;
    });
  }, []);

  const follow = useCallback(
    async (missionId: string, quiet = false) => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setRunning(true);
      setLog([]);
      await streamMission(
        missionId,
        {
          onSnapshot: (m) => {
            setActive(m);
            mergeMissionSystems(m.systems);
            if (m.log?.length) setLog(m.log);
          },
          onSystem: (s) => applySystem(s),
          onLog: (d) => setLog((l) => [...l, d]),
          onDone: (m) => {
            setActive(m);
            mergeMissionSystems(m.systems);
            setRunning(false);
            historyQ.refetch();
          },
          onError: (msg) => {
            // A reconnect is best-effort: if the mission can no longer be streamed (it
            // finished and aged out, or was reaped after a restart) just stop quietly —
            // don't flash a scary "Mission not found." banner over a healthy board.
            if (!quiet) setErr(msg);
            setRunning(false);
          },
        },
        ctrl.signal,
      );
    },
    [applySystem, mergeMissionSystems, historyQ],
  );

  const launch = useCallback(
    async (only?: string[]) => {
      if (!workloadId) return;
      setErr("");
      try {
        const { mission } = await api.runMission({ workload_id: workloadId, systems: only, force: force || !!only });
        setActive(mission);
        mergeMissionSystems(mission.systems);
        await follow(mission.id);
      } catch (e) {
        setErr(formatError(e));
        setRunning(false);
      }
    },
    [workloadId, force, follow, mergeMissionSystems],
  );

  const cancel = useCallback(async () => {
    if (!active?.id) return;
    try {
      await api.cancelMission(active.id);
    } catch {
      /* the stream's done/abort will settle the UI regardless */
    }
  }, [active]);

  // Delete a past mission from history (running/queued ones must be cancelled first). Clears the
  // confirm prompt and the active selection if it was the one removed.
  const removeMission = useCallback(async (id: string) => {
    try {
      await api.deleteMission(id);
      setConfirmDelete((d) => (d === id ? "" : d));
      setActive((m) => (m?.id === id ? null : m));
      await historyQ.refetch();
    } catch (e) {
      setErr(formatError(e));
    }
  }, [historyQ]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Reconnect to an in-flight mission on mount (the mission keeps running server-side even
  // if this screen was unmounted — navigating away + back, a network blip, or an HMR reload
  // must not leave the board stuck on a stale "Mission in progress…"). A direct fetch is used
  // (not the cached history query, which has a staleTime that would hide a just-launched run).
  // The `cancelled` flag makes this correct under React StrictMode's mount→unmount→remount.
  useEffect(() => {
    if (!workloadId) return;
    let cancelled = false;
    api
      .listMissions(workloadId, 1)
      .then((r) => {
        const latest = r.missions?.[0];
        if (!cancelled && latest && (latest.status === "running" || latest.status === "queued")) {
          setActive(latest);
          mergeMissionSystems(latest.systems);
          void follow(latest.id, true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workloadId]);

  // The board is always the full set of systems (merged with any active mission deltas), so
  // the readiness ring reflects the whole workload — not just the systems of the last run.
  const board = systems;
  const wlName = stateQ.data?.workload_name || active?.workload_name || "Workload";

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-4 pt-3 pb-3">
        <div className="mb-1 flex items-center gap-2 text-xs text-gray-500">
          <button onClick={() => navigate("/mission-control")} className="hover:text-brand">← Mission Control</button>
          <span>/</span>
          <span className="truncate">{wlName}</span>
        </div>
        <div className="flex flex-wrap items-center gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-lg">🚀</span>
              <h1 className="truncate text-lg font-semibold text-gray-900">{wlName}</h1>
              {connName && (
                <span className="rounded-md border bg-white px-2 py-0.5 text-xs text-gray-600">🏢 {connName}</span>
              )}
            </div>
            <p className="text-xs text-gray-500">Run every analysis for this workload, then dive into any system.</p>
          </div>
          <div className="ml-auto flex items-center gap-3">
            <ReadinessRing systems={board} />
            <div className="flex flex-col items-end gap-1.5">
              <div className="flex items-center gap-2">
                {running && (
                  <button
                    onClick={cancel}
                    className="rounded-lg border border-red-300 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50"
                  >
                    Cancel
                  </button>
                )}
                <button
                  onClick={() => launch()}
                  disabled={running}
                  className="inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-50"
                >
                  {running ? <><Spinner className="h-4 w-4" /> Mission in progress…</> : "🚀 Launch full sweep"}
                </button>
              </div>
              <label className="flex items-center gap-1.5 text-xs text-gray-600">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                Force re-run (ignore fresh)
              </label>
            </div>
          </div>
        </div>
      </div>

      {err && <div className="border-b bg-red-50 px-4 py-2 text-sm text-red-700">{err}</div>}

      <div className="flex min-h-0 flex-1 gap-4 overflow-auto p-4">
        {/* Systems board */}
        <div className="min-w-0 flex-1 space-y-2">
          {board.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
              {stateQ.isLoading ? "Loading…" : stateQ.data?.error || "No systems to show."}
            </div>
          ) : (
            board.map((s) => <SystemTile key={s.key} system={s} onRun={(key) => launch([key])} busy={running} />)
          )}
        </div>

        {/* Side rail: log + history */}
        <div className="hidden w-80 shrink-0 space-y-4 lg:block">
          <div className="rounded-xl border bg-white">
            <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Mission log</div>
            <div ref={logRef} className="max-h-64 overflow-auto p-2 font-mono text-[11px] text-gray-600">
              {log.length === 0 ? (
                <div className="px-1 py-2 text-gray-400">No activity yet. Launch a sweep.</div>
              ) : (
                log.slice(-80).map((l, i) => (
                  <div key={i} className="py-0.5">
                    <span className="text-gray-400">{new Date(l.ts).toLocaleTimeString()}</span> {l.message}
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="rounded-xl border bg-white">
            <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Mission history</div>
            <div className="max-h-72 overflow-auto">
              {(historyQ.data?.missions ?? []).length === 0 ? (
                <div className="px-3 py-3 text-xs text-gray-400">No past missions.</div>
              ) : (
                (historyQ.data?.missions ?? []).map((m) => {
                  const meta = READINESS_META[m.readiness] ?? READINESS_META.unknown;
                  const live = m.status === "running" || m.status === "queued";
                  return (
                    <div key={m.id} className="group flex items-center gap-2 border-b px-3 py-2 last:border-0">
                      <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: meta.ring }} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs font-medium text-gray-700">{meta.label}</div>
                        <div className="text-[10px] text-gray-400">
                          {m.started_at ? new Date(m.started_at).toLocaleString() : ""} · {m.systems_done}/{m.systems_total}
                        </div>
                      </div>
                      <span className="shrink-0 rounded bg-gray-100 px-1 text-[10px] text-gray-500">{m.status}</span>
                      {confirmDelete === m.id ? (
                        <span className="flex shrink-0 items-center gap-1">
                          <button
                            onClick={() => void removeMission(m.id)}
                            className="rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-medium text-white hover:bg-red-700"
                            title="Confirm delete"
                          >Delete</button>
                          <button
                            onClick={() => setConfirmDelete("")}
                            className="rounded border px-1.5 py-0.5 text-[10px] text-gray-500 hover:bg-gray-50"
                          >Cancel</button>
                        </span>
                      ) : (
                        <button
                          onClick={() => setConfirmDelete(m.id)}
                          disabled={live}
                          className="shrink-0 rounded px-1 py-0.5 text-[11px] text-gray-300 hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-30 group-hover:text-gray-400"
                          title={live ? "Cancel the mission before deleting" : "Delete this mission from history"}
                        >✕</button>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// Landing page (the top-level /mission-control route): pick a workload to open its board.
// Shows each workload's last-mission readiness so it doubles as a fleet overview.
function MissionLanding() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const missionsQ = useQuery({ queryKey: ["missions", "all"], queryFn: () => api.listMissions(undefined, 200) });
  const workloads = wlQ.data?.workloads ?? [];
  const [launchingId, setLaunchingId] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const [err, setErr] = useState("");

  // Latest mission per workload (listMissions is newest-first).
  const latestByWorkload = useMemo(() => {
    const m = new Map<string, Mission>();
    for (const mi of missionsQ.data?.missions ?? []) {
      if (!m.has(mi.workload_id)) m.set(mi.workload_id, mi);
    }
    return m;
  }, [missionsQ.data]);

  // Launch a full sweep straight from the landing page, then open the board to watch it stream.
  async function launch(id: string) {
    if (launchingId) return;
    setErr(""); setLaunchingId(id);
    try {
      await api.runMission({ workload_id: id });
      navigate(`/mission-control/${id}`);
    } catch (e) {
      setErr(formatError(e));
      setLaunchingId("");
    }
  }

  // Delete a workload's entire Mission Control (all its mission runs). No trash — permanent.
  async function remove(id: string, name: string) {
    if (deletingId) return;
    if (!window.confirm(`Delete the Mission Control for “${name}”? This permanently removes all of its mission runs and history. This can't be undone.`)) return;
    setErr(""); setDeletingId(id);
    try {
      await api.deleteWorkloadMissions(id);
      await qc.invalidateQueries({ queryKey: ["missions", "all"] });
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setDeletingId("");
    }
  }

  // Split workloads into those that already have a Mission Control vs. those that don't yet.
  const withMC = workloads.filter((w) => latestByWorkload.has(w.id));
  const withoutMC = workloads.filter((w) => !latestByWorkload.has(w.id));

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <div className="border-b bg-white px-6 pt-4 pb-4">
        <div className="flex items-center gap-2">
          <span className="text-xl">🚀</span>
          <h1 className="text-lg font-semibold text-gray-900">Mission Control</h1>
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Pick a workload to run every analysis for it — architecture, assessment, monitoring,
          telemetry, backup, performance and retirement — then dive into any system.
        </p>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-6">
        {err && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
        {wlQ.isLoading ? (
          <div className="text-sm text-gray-500">Loading…</div>
        ) : workloads.length === 0 ? (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
            No workloads yet. Create one under{" "}
            <button onClick={() => navigate("/workloads")} className="font-medium text-brand hover:underline">
              Azure Workloads
            </button>{" "}
            first.
          </div>
        ) : (
          <div className="space-y-8">
            {/* Workloads that already have a Mission Control. */}
            <section>
              <div className="mb-3 flex items-baseline gap-2">
                <h2 className="text-sm font-semibold text-gray-700">Mission Controls</h2>
                <span className="text-xs text-gray-400">{withMC.length} workload{withMC.length === 1 ? "" : "s"}</span>
              </div>
              {withMC.length === 0 ? (
                <p className="rounded-lg border border-dashed p-6 text-center text-xs text-gray-400">
                  No Mission Controls yet. Create one for a workload below.
                </p>
              ) : (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {withMC.map((w) => {
                    const mi = latestByWorkload.get(w.id);
                    const meta = mi ? READINESS_META[mi.readiness] ?? READINESS_META.unknown : null;
                    const count = w.summary?.total_resources ?? w.nodes?.length ?? 0;
                    const launching = launchingId === w.id;
                    const deleting = deletingId === w.id;
                    return (
                      <div
                        key={w.id}
                        className="flex flex-col gap-2 rounded-xl border bg-white p-4 shadow-sm transition hover:border-brand/40 hover:shadow"
                      >
                        <button onClick={() => navigate(`/mission-control/${w.id}`)} className="flex flex-col gap-2 text-left">
                          <div className="flex items-start justify-between gap-2">
                            <span className="truncate font-semibold text-gray-800">{w.name}</span>
                            {meta && (
                              <span className="inline-flex shrink-0 items-center gap-1 text-xs font-medium" style={{ color: meta.ring }}>
                                <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: meta.ring }} />
                                {meta.label}
                              </span>
                            )}
                          </div>
                          <p className="line-clamp-2 text-xs text-gray-500">{w.description || "—"}</p>
                          <div className="mt-1 flex items-center justify-between text-[11px] text-gray-400">
                            <span>{count} resource{count === 1 ? "" : "s"}</span>
                            <span>
                              {mi && mi.started_at ? `last mission ${new Date(mi.started_at).toLocaleDateString()}` : "no missions yet"}
                            </span>
                          </div>
                        </button>
                        <div className="mt-1 flex items-center gap-2">
                          <button
                            onClick={() => void launch(w.id)}
                            disabled={!!launchingId || deleting}
                            className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
                            title="Run every analysis for this workload now"
                          >
                            {launching ? "Launching…" : "🚀 Run sweep"}
                          </button>
                          <button
                            onClick={() => navigate(`/mission-control/${w.id}`)}
                            className="rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-600 transition hover:bg-gray-50"
                          >
                            Open →
                          </button>
                          <button
                            onClick={() => void remove(w.id, w.name)}
                            disabled={!!deletingId || launching}
                            className="ml-auto rounded-lg border border-red-200 px-2.5 py-1.5 text-xs font-medium text-red-600 transition hover:bg-red-50 disabled:opacity-50"
                            title="Delete this workload's Mission Control (all mission runs) — permanent, no trash"
                          >
                            {deleting ? "Deleting…" : "🗑 Delete"}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>

            {/* Workloads without a Mission Control yet — create one. */}
            {withoutMC.length > 0 && (
              <section>
                <div className="mb-3 flex items-baseline gap-2">
                  <h2 className="text-sm font-semibold text-gray-700">No Mission Control yet</h2>
                  <span className="text-xs text-gray-400">{withoutMC.length} workload{withoutMC.length === 1 ? "" : "s"}</span>
                </div>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {withoutMC.map((w) => {
                    const count = w.summary?.total_resources ?? w.nodes?.length ?? 0;
                    const launching = launchingId === w.id;
                    return (
                      <div
                        key={w.id}
                        className="flex flex-col gap-2 rounded-xl border border-dashed bg-white p-4 shadow-sm transition hover:border-brand/40 hover:shadow"
                      >
                        <div className="flex flex-col gap-2">
                          <div className="flex items-start justify-between gap-2">
                            <span className="truncate font-semibold text-gray-800">{w.name}</span>
                            <span className="shrink-0 text-[11px] text-gray-400">not set up</span>
                          </div>
                          <p className="line-clamp-2 text-xs text-gray-500">{w.description || "—"}</p>
                          <div className="mt-1 text-[11px] text-gray-400">{count} resource{count === 1 ? "" : "s"}</div>
                        </div>
                        <div className="mt-1">
                          <button
                            onClick={() => void launch(w.id)}
                            disabled={!!launchingId}
                            className="w-full rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
                            title="Create a Mission Control for this workload by running its first sweep"
                          >
                            {launching ? "Creating…" : "＋ Create Mission Control"}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// Route entry: /mission-control = landing (workload picker), /mission-control/:id = the board.
export function MissionControlPanel() {
  const { id } = useParams<{ id: string }>();
  return id ? <MissionBoard workloadId={id} /> : <MissionLanding />;
}

