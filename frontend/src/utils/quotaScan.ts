// Module-level quota-scan registry (QP5). A quota scan is a streaming SSE collection that can run
// for a while (regions × collectors). Keeping its state HERE — not in the QuotaView component —
// means the scan survives navigating away and back: the fetch keeps streaming, progress keeps
// accumulating, and the component re-subscribes via useSyncExternalStore on remount. The view can
// then minimise the progress popup into a header chip instead of a blocking modal. Mirrors the
// Performance-Profiler / coverage background-refresh pattern.
import { useSyncExternalStore } from "react";
import { queryClient } from "../queryClient";
import { streamQuotaScan, type QuotaScanParams, type QuotaSnapshot } from "../api";
import { formatError } from "./format";

export type QuotaScanLogLine = { t: string; phase: string; msg: string };
export type QuotaScanState = {
  scanning: boolean;
  status: string;
  log: QuotaScanLogLine[];
  startedAt: number;
  subLabel: string;
  regionLabel: string;
  error: string;
  done: boolean; // finished successfully (snapshot landed in the query cache)
  regionsScanned: number;
  finishedAt: number; // timestamp of completion (lets the view fire a one-shot "done" effect)
};

type Entry = { state: QuotaScanState; abort: AbortController };

const _scans = new Map<string, Entry>();
let _version = 0;
const _listeners = new Set<() => void>();

function _bump() {
  _version += 1;
  for (const l of _listeners) l();
}

function _push(key: string, phase: string, msg: string) {
  const e = _scans.get(key);
  if (!e) return;
  const t = new Date().toLocaleTimeString([], { hour12: false });
  e.state.status = msg;
  e.state.log = [...e.state.log.slice(-299), { t, phase, msg }];
}

/** Subscribe a component to scan-registry changes (monotonic version). */
export function useQuotaScanVersion(): number {
  return useSyncExternalStore(
    (cb) => { _listeners.add(cb); return () => { _listeners.delete(cb); }; },
    () => _version,
    () => _version,
  );
}

export function getQuotaScan(key: string): QuotaScanState | null {
  return _scans.get(key)?.state ?? null;
}

/** Start a streaming quota scan for `key`. No-op if one is already running for that key. The
 *  result is written to `cacheKey` in the shared query cache so the (enabled:false) overview
 *  query reflects it even if the component was unmounted during the scan. */
export function startQuotaScan(
  key: string,
  params: QuotaScanParams,
  cacheKey: readonly unknown[],
  opts: { subLabel: string; regionLabel: string } = { subLabel: "", regionLabel: "" },
): void {
  if (_scans.get(key)?.state.scanning) return;
  const abort = new AbortController();
  const state: QuotaScanState = {
    scanning: true, status: "Starting…", log: [], startedAt: Date.now(),
    subLabel: opts.subLabel, regionLabel: opts.regionLabel, error: "", done: false,
    regionsScanned: 0, finishedAt: 0,
  };
  _scans.set(key, { state, abort });
  _push(key, "start", "🚀 Starting quota scan…");
  _bump();

  void streamQuotaScan(
    params,
    {
      onStatus: (s) => { _push(key, s.phase, s.message); _bump(); },
      onDone: (fresh: QuotaSnapshot) => {
        queryClient.setQueryData(cacheKey, fresh);
        if (params.subscription_id) {
          void queryClient.invalidateQueries({ queryKey: ["quotaRuns", params.subscription_id] });
        }
        const e = _scans.get(key);
        if (e) {
          e.state.scanning = false;
          e.state.error = fresh.error || "";
          e.state.done = !fresh.error;
          e.state.regionsScanned = fresh.regions_scanned?.length ?? 0;
          e.state.finishedAt = Date.now();
        }
        if (fresh.error) _push(key, "error", `❌ ${fresh.error}`);
        _bump();
      },
      onError: (m: string) => {
        const e = _scans.get(key);
        if (e) { e.state.scanning = false; e.state.error = m; e.state.finishedAt = Date.now(); }
        _push(key, "error", `❌ ${m}`);
        _bump();
      },
    },
    abort.signal,
  ).catch((e) => {
    const entry = _scans.get(key);
    if (entry) { entry.state.scanning = false; entry.state.error = formatError(e); entry.state.finishedAt = Date.now(); }
    _push(key, "error", `❌ ${formatError(e)}`);
    _bump();
  });
}

export function cancelQuotaScan(key: string): void {
  const e = _scans.get(key);
  if (!e) return;
  e.abort.abort();
  e.state.scanning = false;
  e.state.finishedAt = Date.now();
  _push(key, "cancel", "⏹️ Cancelled.");
  _bump();
}

/** Drop a finished scan's state (after the view has consumed it). No-op while still running. */
export function clearQuotaScan(key: string): void {
  const e = _scans.get(key);
  if (e && !e.state.scanning) { _scans.delete(key); _bump(); }
}
