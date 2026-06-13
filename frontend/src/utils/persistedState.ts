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
