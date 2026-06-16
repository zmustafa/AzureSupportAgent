import { useSyncExternalStore } from "react";
import { formatError } from "./format";

// ---- Shared background-refresh registry -----------------------------------------
// A coverage/posture refresh (a forced Azure scan) should keep running even if the user
// switches scope or navigates away — and the button state must be PER-SCOPE so a scope
// with no active refresh shows "↻ Refresh now", not "Refreshing…". This module-level
// registry (keyed by an arbitrary string, e.g. "amba:workload:<id>") tracks in-flight
// refreshes independently of any component's lifecycle. The actual work — including the
// react-query cache update via the shared queryClient — lives in the caller's `run`
// callback, so it completes regardless of which screen is mounted. Shared by the
// Monitoring / Telemetry / Backup-DR coverage screens.

const _running = new Map<string, { startedAt: number }>();
const _errors = new Map<string, string>();
let _version = 0;
const _listeners = new Set<() => void>();

function _bump() {
  _version += 1;
  for (const l of _listeners) l();
}

// Subscribe a component to registry changes; returns a monotonically-increasing version
// so render + effects react to any start/finish anywhere.
export function useBackgroundRefresh(): number {
  return useSyncExternalStore(
    (cb) => {
      _listeners.add(cb);
      return () => {
        _listeners.delete(cb);
      };
    },
    () => _version,
    () => _version,
  );
}

export function isRefreshing(key: string): boolean {
  return _running.has(key);
}

// Pull (and clear) the last error recorded for a key, so the originating screen can show
// it once even if the failure happened while it was unmounted.
export function takeRefreshError(key: string): string | null {
  const e = _errors.get(key);
  if (e !== undefined) {
    _errors.delete(key);
    return e;
  }
  return null;
}

// Start a background refresh for `key`. No-op if one is already running for that key.
// `run` performs the fetch + cache update (via the shared queryClient) and may throw —
// the error is captured for the originating screen to surface.
export function startBackgroundRefresh(key: string, run: () => Promise<void>): void {
  if (_running.has(key)) return;
  _errors.delete(key);
  _running.set(key, { startedAt: Date.now() });
  _bump();
  void (async () => {
    try {
      await run();
    } catch (e) {
      _errors.set(key, formatError(e));
    } finally {
      _running.delete(key);
      _bump();
    }
  })();
}
