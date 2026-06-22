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
