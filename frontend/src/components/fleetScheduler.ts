// Shared, MODULE-LEVEL fleet launch scheduler.
//
// Both the Performance Profiler and Change Explorer "Fleet" views mass-launch background jobs
// (profile / analyze) across many workloads at a bounded concurrency. Previously each view kept
// its pending queue in COMPONENT state + a scheduler useEffect — so switching tabs / navigating
// away unmounted the component and SILENTLY DROPPED every not-yet-started job (only the in-flight
// ones, held by the module-level run registries, survived). It also caused a refetch storm.
//
// This module hoists the queue to MODULE scope and SELF-DRIVES it: each active queue subscribes
// to its run-registry's change notifications and re-drains itself whenever a run starts/finishes,
// independent of any mounted component. The queue therefore survives navigation, and components
// only need to (a) call `enqueueFleet(...)` and (b) read `fleetQueuedKeys` / `fleetRunningCount`
// for display (reactive via `useFleetQueue`).
import { useSyncExternalStore } from "react";

export interface FleetJob {
  /** Unique scope key for the job, e.g. `workload:<id>`. Used for dedupe + running checks. */
  key: string;
  /** Start the job (fire-and-forget; must register itself so `isRunning(key)` flips true). */
  run: () => void;
}

interface QueueState {
  pending: FleetJob[];
  started: Set<string>;
  maxParallel: number;
  isRunning: (key: string) => boolean;
  unsubscribe?: () => void;
}

const _queues = new Map<string, QueueState>();
let _version = 0;
const _subs = new Set<() => void>();
function _bump() {
  _version += 1;
  for (const s of _subs) s();
}

/** Subscribe a component to queue changes (queued keys / running counts). */
export function useFleetQueue(): number {
  return useSyncExternalStore(
    (cb) => {
      _subs.add(cb);
      return () => {
        _subs.delete(cb);
      };
    },
    () => _version,
    () => _version,
  );
}

/** The set of keys currently WAITING (not yet started) in a queue. */
export function fleetQueuedKeys(queueId: string): Set<string> {
  const q = _queues.get(queueId);
  return new Set(q ? q.pending.map((j) => j.key) : []);
}

/** How many jobs this queue has launched that are still running. */
export function fleetRunningCount(queueId: string): number {
  return _queues.get(queueId)?.started.size ?? 0;
}

/** Total jobs still outstanding (queued + running) — drives "N running" + button disable. */
export function fleetOutstanding(queueId: string): number {
  const q = _queues.get(queueId);
  return q ? q.pending.length + q.started.size : 0;
}

/**
 * Enqueue jobs into a named queue and (re)start draining. Idempotent per key: a key already
 * queued or already running is skipped, so double-clicking Launch can't double-run a workload.
 *
 * @param subscribe the run-registry's change subscription — lets the queue self-drive (re-drain
 *                  when a run finishes) even while no component is mounted.
 */
export function enqueueFleet(
  queueId: string,
  jobs: FleetJob[],
  opts: {
    maxParallel: number;
    isRunning: (key: string) => boolean;
    subscribe: (cb: () => void) => () => void;
  },
): void {
  let q = _queues.get(queueId);
  if (!q) {
    q = { pending: [], started: new Set(), maxParallel: opts.maxParallel, isRunning: opts.isRunning };
    _queues.set(queueId, q);
    // Self-drive: re-drain on every run-registry change until the queue empties.
    q.unsubscribe = opts.subscribe(() => _drain(queueId));
  }
  q.maxParallel = opts.maxParallel;
  q.isRunning = opts.isRunning;
  for (const j of jobs) {
    if (q.pending.some((p) => p.key === j.key)) continue;
    if (q.started.has(j.key)) continue;
    if (opts.isRunning(j.key)) continue;
    q.pending.push(j);
  }
  _drain(queueId);
}

function _drain(queueId: string): void {
  const q = _queues.get(queueId);
  if (!q) return;
  // Reap jobs we started that are no longer running.
  for (const k of [...q.started]) {
    if (!q.isRunning(k)) q.started.delete(k);
  }
  // Fill open slots from the pending queue.
  const slots = Math.max(0, q.maxParallel - q.started.size);
  for (let i = 0; i < slots && q.pending.length > 0; i++) {
    const job = q.pending.shift();
    if (!job) break;
    q.started.add(job.key);
    try {
      job.run();
    } catch {
      q.started.delete(job.key);
    }
  }
  // Tear the queue down once fully drained so a future batch re-subscribes cleanly.
  if (q.pending.length === 0 && q.started.size === 0) {
    q.unsubscribe?.();
    _queues.delete(queueId);
  }
  _bump();
}
