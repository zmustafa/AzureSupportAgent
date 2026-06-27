// "New version available" prompt. Subscribes to the PWA update store and shows a small,
// dismissible toast with a Reload button when a new deploy is detected. Non-destructive:
// the user chooses when to reload, so an in-progress chat / unsaved form isn't interrupted.
import { useSyncExternalStore, useState } from "react";
import { subscribeUpdate, getUpdateReady, applyUpdate } from "../pwa";

export function UpdateBanner() {
  const ready = useSyncExternalStore(subscribeUpdate, getUpdateReady, getUpdateReady);
  const [dismissed, setDismissed] = useState(false);
  if (!ready || dismissed) return null;

  return (
    <div className="fixed bottom-4 left-1/2 z-[100] -translate-x-1/2">
      <div className="flex items-center gap-3 rounded-xl border border-brand/30 bg-white px-4 py-2.5 shadow-lg">
        <span className="text-lg">✨</span>
        <div className="text-sm text-gray-700">
          <div className="font-medium text-gray-900">A new version is available</div>
          <div className="text-xs text-gray-500">Reload to get the latest features and fixes.</div>
        </div>
        <button
          onClick={() => applyUpdate()}
          className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand/90"
        >
          Reload
        </button>
        <button
          onClick={() => setDismissed(true)}
          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          title="Dismiss (you can reload later)"
        >
          ✕
        </button>
      </div>
    </div>
  );
}
