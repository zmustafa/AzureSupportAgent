// Service-worker update detection + version-poll safety net.
//
// PROBLEM this solves: with `registerType: autoUpdate` the browser only checks for a new
// service worker on a FULL navigation. A long-open SPA tab that only does client-side
// (React Router) navigation therefore keeps running the OLD precached bundle after a deploy
// — the version pill lags (e.g. shows v67 while v69 is live) until a manual hard refresh.
//
// FIX (three layers):
//   A) Register the SW manually and poll `registration.update()` on an interval + on tab
//      focus, so a new deploy is detected within ~1 min. On `onNeedRefresh`, surface a
//      non-destructive "Update available" prompt (never auto-reload mid-session — that could
//      drop an unsent chat message or unsaved form).
//   C) (in vite.config) navigations use NetworkFirst so even a plain reload picks up fresh HTML.
//   D) Independently poll the unauthenticated `/version` endpoint and compare it to the baked
//      APP_VERSION; if they differ, raise the same prompt. This is SW-independent defense in
//      depth (catches a stale/odd SW state).
import { registerSW } from "virtual:pwa-register";
import { apiBase } from "./api";
import { APP_VERSION } from "./version";

// How often to check for a new SW / poll the server version (ms).
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

// The SW's "apply the waiting worker + reload" function, wired up by registerSW below.
let _applySW: ((reload?: boolean) => Promise<void>) | null = null;

/** Apply a pending update and reload onto the new bundle. Called from the UI prompt. */
export function applyUpdate(): void {
  if (_applySW) {
    // updateSW(true) activates the waiting worker (skipWaiting) and reloads the page.
    void _applySW(true);
  } else {
    // No waiting SW (e.g. the version-poll fired first) — a plain reload fetches fresh HTML
    // (NetworkFirst) and the newest assets.
    window.location.reload();
  }
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

/** Wire up SW registration, periodic update checks, and the version-poll safety net. */
export function setupPWA(): void {
  let swRegistration: ServiceWorkerRegistration | undefined;

  _applySW = registerSW({
    immediate: true,
    onNeedRefresh() {
      // A new SW has installed and is waiting — prompt the user.
      _markUpdateReady();
    },
    onRegisteredSW(_swUrl, registration) {
      swRegistration = registration;
    },
  });

  const checkForUpdate = () => {
    // Ask the SW to re-check for a new version, and independently poll the server version.
    try {
      void swRegistration?.update();
    } catch {
      /* ignore */
    }
    void _pollServerVersion();
  };

  // Periodic check + on tab focus / regained visibility (a tab left open for hours).
  window.setInterval(checkForUpdate, CHECK_INTERVAL_MS);
  window.addEventListener("focus", checkForUpdate);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkForUpdate();
  });

  // One eager poll shortly after load so a tab opened just after a deploy catches up fast.
  window.setTimeout(() => void _pollServerVersion(), 3_000);
}
