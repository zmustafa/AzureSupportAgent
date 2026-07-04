// Adds a top-level "Overview | 🧹 Cleanup" tab bar to a screen that doesn't otherwise have
// tabs (Assessments + the 3 Coverage screens). Renders the screen's normal panel on the
// Overview tab and the shared RunCleanup on the Cleanup tab. The screen always OPENS on
// Overview (its primary content) — the Cleanup tab is a secondary, destructive-adjacent
// view, so we deliberately don't persist it as the landing tab. The choice still holds
// while you're on the page; navigating away and back resets to Overview.
import { useState } from "react";
import { RunCleanup } from "./RunCleanup";
import type { CleanupRun } from "../../api";

export function CleanupTabShell({
  overviewLabel,
  prefix,
  queryKey,
  invalidateKeys,
  renderMeta,
  isEmptyRun,
  children,
}: {
  /** Retained for API compatibility with callers; no longer used for persistence. */
  storageKey?: string;
  overviewLabel: string;
  prefix: string;
  queryKey: readonly unknown[];
  invalidateKeys?: readonly unknown[][];
  renderMeta: (r: CleanupRun) => React.ReactNode;
  isEmptyRun?: (r: CleanupRun) => boolean;
  children: React.ReactNode;
}) {
  const [view, setView] = useState<"overview" | "cleanup">("overview");
  // Mount the overview lazily-kept (display:none) so its in-flight state survives a tab peek.
  const [touchedCleanup, setTouchedCleanup] = useState(false);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b bg-white px-5 pt-2">
        {(["overview", "cleanup"] as const).map((v) => (
          <button
            key={v}
            onClick={() => { setView(v); if (v === "cleanup") setTouchedCleanup(true); }}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${view === v ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}
          >
            {v === "overview" ? overviewLabel : "🧹 Cleanup"}
          </button>
        ))}
      </div>
      <div className={`min-h-0 flex-1 ${view === "overview" ? "flex flex-col" : "hidden"}`}>{children}</div>
      {(view === "cleanup" || touchedCleanup) && (
        <div className={`min-h-0 flex-1 ${view === "cleanup" ? "flex flex-col" : "hidden"}`}>
          <RunCleanup prefix={prefix} queryKey={queryKey} invalidateKeys={invalidateKeys} renderMeta={renderMeta} isEmptyRun={isEmptyRun} />
        </div>
      )}
    </div>
  );
}
