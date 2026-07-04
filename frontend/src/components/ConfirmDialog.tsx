import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------------
// Accessible, promise-based confirmation dialog — a consistent, keyboard-first upgrade
// path from scattered native `window.confirm(...)` calls.
//
// Usage:
//   const confirm = useConfirm();
//   if (!(await confirm({ title: "Delete?", message: "…", destructive: true }))) return;
//
// Guarantees: role="dialog" + aria-modal, an accessible name from the title, focus lands
// on the SAFE (Cancel) button, Escape cancels, and destructive actions get a red confirm
// button. Native confirm() remains valid elsewhere; migrate the scariest flows first.
// ---------------------------------------------------------------------------------

export type ConfirmOptions = {
  title?: string;
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Red confirm button + "this can't be undone" framing. */
  destructive?: boolean;
};

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** Returns an async confirm() that resolves true (confirmed) or false (cancelled). */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within <ConfirmProvider>");
  return ctx;
}

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{ opts: ConfirmOptions; resolve: (v: boolean) => void } | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (opts) => new Promise<boolean>((resolve) => setState({ opts, resolve })),
    [],
  );

  const close = useCallback((result: boolean) => {
    setState((s) => {
      s?.resolve(result);
      return null;
    });
    previouslyFocused.current?.focus?.();
  }, []);

  useEffect(() => {
    if (state) {
      previouslyFocused.current = document.activeElement as HTMLElement | null;
      // Focus the SAFE default so an accidental Enter cancels rather than confirms.
      cancelRef.current?.focus();
    }
  }, [state]);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {state && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-dialog-title"
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 px-4 backdrop-blur-[1px]"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              close(false);
            }
          }}
          onMouseDown={(e) => {
            // Click on the backdrop (not the panel) cancels.
            if (e.target === e.currentTarget) close(false);
          }}
        >
          <div className="w-full max-w-md overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl">
            <div className="px-5 py-4">
              <h2 id="confirm-dialog-title" className="text-base font-semibold text-gray-900">
                {state.opts.title ?? "Are you sure?"}
              </h2>
              <div className="mt-2 text-sm text-gray-600">{state.opts.message}</div>
            </div>
            <div className="flex justify-end gap-2 border-t bg-gray-50 px-5 py-3">
              <button
                ref={cancelRef}
                onClick={() => close(false)}
                className="rounded-lg border px-3 py-1.5 text-sm text-gray-700 transition hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-brand/40"
              >
                {state.opts.cancelLabel ?? "Cancel"}
              </button>
              <button
                onClick={() => close(true)}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium text-white transition focus:outline-none focus:ring-2 ${
                  state.opts.destructive
                    ? "bg-red-600 hover:bg-red-700 focus:ring-red-400"
                    : "bg-brand hover:bg-brand/90 focus:ring-brand/40"
                }`}
              >
                {state.opts.confirmLabel ?? "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
