import { useEffect, useState } from "react";

/**
 * Like useState, but persists the value to localStorage under `key` so it survives
 * navigation and reloads. Used by the coverage / radar / telemetry-intel / performance
 * screens to remember the last selected workload (scope) the user was looking at.
 */
export function usePersistedState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw != null) return JSON.parse(raw) as T;
    } catch {
      /* ignore malformed / unavailable storage */
    }
    return initial;
  });

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* ignore quota / unavailable storage */
    }
  }, [key, value]);

  return [value, setValue];
}

/**
 * One-shot rename of a persisted key. Call at module scope before the hook runs.
 *
 * Without this a renamed screen silently drops the user's stored selection. For a scope or
 * connection picker that is worse than an error: the screen quietly falls back to the default
 * connection, so the reader can end up looking at another tenant's data without noticing.
 * No-op when the new key already exists, so a user who has since made a choice keeps it.
 */
export function migratePersistedKey(oldKey: string, newKey: string): void {
  try {
    if (localStorage.getItem(newKey) != null) {
      localStorage.removeItem(oldKey);
      return;
    }
    const raw = localStorage.getItem(oldKey);
    if (raw == null) return;
    localStorage.setItem(newKey, raw);
    localStorage.removeItem(oldKey);
  } catch {
    /* ignore quota / unavailable storage */
  }
}

/**
 * The Azure connection (tenant) the user is looking at, shared by every section.
 *
 * This used to be per-section: IAM, Entra, Inventory, Policy, Ownership and the Estate Graph
 * each remembered their own. Switching tenant on one screen therefore left every other screen
 * on the previous one — so a user who switched to another tenant in IAM and then opened Entra
 * was shown the OLD tenant's posture, under a picker that said so only if they looked. "Switch
 * tenant" is a statement about the session, not about one page.
 *
 * Sections are route-level and remount on navigation, and `usePersistedState` reads storage on
 * mount, so sharing the key is enough to keep them in step without any cross-component plumbing.
 */
export const CONNECTION_KEY = "azsup.connectionId";

/** The per-section keys this replaced, newest-intent first. */
const LEGACY_CONNECTION_KEYS = [
  "azsup.iam.connectionId",
  "azsup.entra.connectionId",
  "azsup.ownership.connectionId",
  "azsup.graph.connection",
  "azsup.rbac.connectionId",
];

/**
 * Fold any previously per-section selection into the shared one.
 *
 * Takes the first non-empty legacy value rather than clearing them, so a user who had picked a
 * tenant keeps looking at it instead of being silently dropped back to the default connection.
 */
export function migrateConnectionKeys(): void {
  try {
    const existing = localStorage.getItem(CONNECTION_KEY);
    if (existing != null && JSON.parse(existing)) return;
    for (const key of LEGACY_CONNECTION_KEYS) {
      const raw = localStorage.getItem(key);
      if (raw != null && JSON.parse(raw)) {
        localStorage.setItem(CONNECTION_KEY, raw);
        return;
      }
    }
  } catch {
    /* ignore malformed / unavailable storage */
  }
}

/**
 * On first mount, if the URL carries `?workload_id=`, switch the screen's scope to that
 * workload. Powers Workload Mission Control deep links (e.g. /coverage?workload_id=…) so
 * the destination opens already scoped to the workload instead of the last-used scope.
 */
export function useWorkloadDeepLink(
  setScopeKind: (k: "workload" | "subscription") => void,
  setWorkloadId: (id: string) => void,
): void {
  useEffect(() => {
    const wid = new URLSearchParams(window.location.search).get("workload_id");
    if (wid) {
      setScopeKind("workload");
      setWorkloadId(wid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
