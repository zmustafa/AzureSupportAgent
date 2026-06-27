// Version-update detection (service-worker-FREE).
//
// The service worker was REMOVED (see vite.config.ts `selfDestroying`) because it caused
// staleness bugs in this online-only app: a still-active OLD worker served STALE lazily-loaded
// route chunks even after a hard refresh, and the "waiting" worker re-prompted endlessly.
//
// Freshness now comes from plain HTTP caching (backend serves index.html no-cache + hashed
// assets immutable) plus this lightweight safety net: poll the unauthenticated `/version`
// endpoint and, when the live build differs from the baked APP_VERSION, show a non-destructive
// "Update available — Reload" banner (long-open tabs still get notified; the user chooses when
// to reload so an in-progress chat/form isn't interrupted).
import { apiBase } from "./api";
import { APP_VERSION } from "./version";

// How often to poll the server version (ms).
const CHECK_INTERVAL_MS = 60_000;

// --- tiny external store so React can subscribe without a context/provider ---------------
let _updateReady = false;
const _listeners = new Set<() => void>();
function _emit() {
  for (const l of _listeners) l();
}
function _markUpdateReady() {
  if (_updateReady) return;
  _updateReady = true;
  _emit();
}
export function subscribeUpdate(cb: () => void): () => void {
  _listeners.add(cb);
  return () => _listeners.delete(cb);
}
export function getUpdateReady(): boolean {
  return _updateReady;
}

/** Apply the update: a plain reload now reliably fetches fresh index.html (no-cache) and the
 *  newest hashed assets, since no service worker intercepts requests anymore. */
export function applyUpdate(): void {
  window.location.reload();
}
function _versionUrl(): string {
  // `/version` lives at the app root (NOT under /api). Derive its origin from the API base so
  // it works same-origin in prod (apiBase "/api") and cross-origin in dev (":8000/api").
  try {
    const base = new URL(apiBase, window.location.origin);
    return new URL("/version", base).toString();
  } catch {
    return "/version";
  }
}

async function _pollServerVersion(): Promise<void> {
  // Only meaningful for a real build; a local `dev` build has no deployed counterpart.
  if (APP_VERSION === "dev") return;
  try {
    const res = await fetch(_versionUrl(), { cache: "no-store", credentials: "omit" });
    if (!res.ok) return;
    const data = (await res.json()) as { version?: string };
    const live = (data.version || "").trim();
    if (live && live !== "dev" && live !== APP_VERSION) {
      _markUpdateReady();
    }
  } catch {
    /* offline / transient — ignore */
  }
}

// Belt-and-suspenders: proactively tear down ANY previously-installed service worker + its
// caches the moment the app loads. The build's `selfDestroying` sw.js already does this on its
// own, but unregistering here too means clients upgrade cleanly even if their cached sw.js is
// momentarily stale. Safe no-op when there's no SW (dev, or already removed).
function _killServiceWorkers(): void {
  if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
    void navigator.serviceWorker.getRegistrations()
      .then((regs) => { for (const r of regs) void r.unregister(); })
      .catch(() => { /* ignore */ });
  }
  if (typeof caches !== "undefined") {
    void caches.keys()
      .then((keys) => { for (const k of keys) void caches.delete(k); })
      .catch(() => { /* ignore */ });
  }
}

/** Wire up the version-poll safety net (no service worker). */
export function setupPWA(): void {
  _killServiceWorkers();

  // Periodic poll + on tab focus / regained visibility (a tab left open for hours).
  window.setInterval(() => void _pollServerVersion(), CHECK_INTERVAL_MS);
  window.addEventListener("focus", () => void _pollServerVersion());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void _pollServerVersion();
  });

  // One eager poll shortly after load so a tab opened just after a deploy catches up fast.
  window.setTimeout(() => void _pollServerVersion(), 3_000);
}
