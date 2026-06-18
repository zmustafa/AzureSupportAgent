import type { ReactNode } from "react";

export type RunHistoryColumn<T> = {
  header: string;
  className?: string;
  /** Right-align (e.g. an actions column). */
  align?: "left" | "right";
  render: (row: T) => ReactNode;
};

/**
 * Shared visual chrome for every "scan / run history" panel — header (title + count + a
 * Trash toggle), an optional status message, the active table, and the Trash panel
 * (restore / delete-forever / empty). Purely presentational: each owner supplies its own
 * columns, rows and action handlers, so the Coverage dashboards and the Performance
 * Profiler render an identical history UI instead of three hand-rolled copies.
 */
export function RunHistoryShell<T extends { id: string }>({
  title = "Scan history",
  countText,
  headerExtra,
  message,
  columns,
  rows,
  loading,
  emptyHint,
  rowClassName,
  prependRow,
  suppressEmpty = false,
  testId = "run-history",
  showTrash,
  onToggleTrash,
  trashedCount,
  trashNote,
  trashColumns,
  trashedRows,
  trashLoading,
  onEmptyTrash,
  emptyingTrash,
}: {
  title?: string;
  countText: string;
  /** Extra node rendered after the count (e.g. a "1 running" badge). */
  headerExtra?: ReactNode;
  message?: { text: string; ok: boolean } | null;
  columns: RunHistoryColumn<T>[];
  rows: T[];
  loading: boolean;
  emptyHint: ReactNode;
  /** Per-row className (e.g. highlight the currently-open run). */
  rowClassName?: (row: T) => string;
  /** A row rendered above the data rows (e.g. a live in-progress run). */
  prependRow?: ReactNode;
  /** Don't show the empty state (e.g. when a prepended live row is present). */
  suppressEmpty?: boolean;
  testId?: string;
  showTrash: boolean;
  onToggleTrash: () => void;
  trashedCount: number;
  trashNote?: ReactNode;
  trashColumns: RunHistoryColumn<T>[];
  trashedRows: T[];
  trashLoading: boolean;
  onEmptyTrash: () => void;
  emptyingTrash: boolean;
}) {
  return (
    <div className="mb-5">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-sm font-semibold text-gray-900">{title}</h2>
        <span className="text-[11px] text-gray-400">{countText}</span>
        {headerExtra}
        <button
          onClick={onToggleTrash}
          title="Show trashed runs"
          className={`ml-auto rounded-md border px-2.5 py-1 text-[11px] font-medium ${showTrash ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
        >
          🗑 Trash{trashedCount ? ` (${trashedCount})` : ""}
        </button>
      </div>

      {message && (
        <div className={`mb-2 rounded-md border px-3 py-1.5 text-xs ${message.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{message.text}</div>
      )}

      <div className="overflow-x-auto rounded-lg border bg-white" data-testid={testId}>
        <table className="w-full text-[12px]">
          <thead className="bg-gray-50 text-left text-gray-500">
            <tr>
              {columns.map((c, i) => (
                <th key={i} className={`px-3 py-2 ${c.align === "right" ? "text-right" : ""} ${c.className ?? ""}`}>{c.header}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {prependRow}
            {loading ? (
              <tr><td colSpan={columns.length} className="px-3 py-4 text-center text-gray-400">Loading history…</td></tr>
            ) : rows.length === 0 && !suppressEmpty ? (
              <tr><td colSpan={columns.length} className="px-3 py-6 text-center text-gray-400">{emptyHint}</td></tr>
            ) : (
              rows.map((r) => (
                <tr key={r.id} className={`border-t hover:bg-gray-50 ${rowClassName ? rowClassName(r) : ""}`}>
                  {columns.map((c, i) => (
                    <td key={i} className={`px-3 py-2 ${c.align === "right" ? "text-right" : ""} ${c.className ?? ""}`}>{c.render(r)}</td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {showTrash && (
        <div className="mt-3 rounded-lg border bg-white">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <div className="flex items-center gap-2">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">🗑 Trash</h3>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">{trashedCount}</span>
            </div>
            {trashedRows.length > 0 && (
              <button onClick={onEmptyTrash} disabled={emptyingTrash} className="rounded-md border border-red-200 px-2.5 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Empty trash</button>
            )}
          </div>
          {trashNote && <p className="border-b px-3 py-1.5 text-[11px] text-gray-500">{trashNote}</p>}
          {trashLoading ? (
            <div className="px-3 py-4 text-center text-sm text-gray-400">Loading…</div>
          ) : trashedRows.length === 0 ? (
            <div className="px-3 py-6 text-center text-sm text-gray-400">Trash is empty.</div>
          ) : (
            <table className="w-full text-[12px]">
              <thead className="bg-gray-50 text-left text-gray-500">
                <tr>
                  {trashColumns.map((c, i) => (
                    <th key={i} className={`px-3 py-2 ${c.align === "right" ? "text-right" : ""} ${c.className ?? ""}`}>{c.header}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trashedRows.map((r) => (
                  <tr key={r.id} className="border-t hover:bg-gray-50">
                    {trashColumns.map((c, i) => (
                      <td key={i} className={`px-3 py-2 ${c.align === "right" ? "text-right" : ""} ${c.className ?? ""}`}>{c.render(r)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
