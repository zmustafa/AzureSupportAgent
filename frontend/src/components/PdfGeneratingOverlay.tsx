/**
 * Full-screen "Generating PDF report" waiting overlay, shared across the views that export a
 * branded PDF (Assessments uses its own inline copy; coverage views + dashboard use this).
 *
 * Renders nothing when `open` is false. When an `onCancel` is provided, a Cancel button is
 * shown (the caller wires it to an AbortController so the in-flight request is aborted).
 */
export function PdfGeneratingOverlay({
  open,
  onCancel,
  title = "Generating PDF report",
  message = "The report is being compiled with the cover, summary, trend, gaps and appendix. You can cancel while it is processing.",
}: {
  open: boolean;
  onCancel?: () => void;
  title?: string;
  message?: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-[1px]">
      <div className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-5 shadow-2xl">
        <div className="flex items-start gap-4">
          <div className="mt-0.5 h-10 w-10 animate-spin rounded-full border-4 border-brand/20 border-t-brand" />
          <div className="min-w-0 flex-1">
            <div className="text-lg font-semibold text-gray-900">{title}</div>
            <p className="mt-1 text-sm text-gray-500">{message}</p>
            {onCancel && (
              <div className="mt-4 flex items-center gap-2">
                <button
                  onClick={onCancel}
                  className="rounded-lg border border-red-200 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100"
                >
                  Cancel
                </button>
                <span className="text-xs text-gray-400">This only cancels the current PDF request.</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
