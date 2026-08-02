/** Download-with-progress dialog for large exports.
 *
 *  A plain `<a href>` download gives no feedback at all: the Entra workbook takes ~8.5s to build
 *  and 5MB to transfer, during which nothing happens on screen. People conclude it is broken and
 *  click again, which starts a second 8-second build on the server.
 *
 *  The progress here is deliberately in TWO honest phases rather than one invented percentage:
 *
 *  1. **Preparing** — the server is building the workbook and has not sent a byte. No percentage
 *     exists yet, so none is shown: an elapsed counter, and how long it took last time if we have
 *     ever finished one. A bar creeping to 90% while the server thinks is a lie that costs
 *     trust the first time somebody notices it stall there.
 *  2. **Downloading** — bytes are arriving and `Content-Length` is known, so the percentage is
 *     real. Without that header (chunked responses) it shows bytes received instead of a made-up
 *     fraction.
 */
import { useCallback, useEffect, useRef, useState } from "react";

type Phase = "idle" | "preparing" | "downloading" | "saving" | "done" | "error";

/** Where the last successful duration is remembered, so the estimate is measured not guessed. */
const DURATION_KEY = "azsup.export.lastMs";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtSeconds(ms: number): string {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export type ExportDownloadState = {
  phase: Phase;
  start: (url: string, filename: string) => void;
  cancel: () => void;
  dialog: React.ReactNode;
};

/** Runs one download at a time and renders its dialog. */
export function useExportDownload(label: string): ExportDownloadState {
  const [phase, setPhase] = useState<Phase>("idle");
  const [received, setReceived] = useState(0);
  const [total, setTotal] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState("");
  const [expected, setExpected] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const startedRef = useRef(0);

  // One ticker for the whole dialog. Runs only while a download is in flight, so an idle screen
  // is not re-rendering once a second forever.
  const running = phase === "preparing" || phase === "downloading" || phase === "saving";
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setElapsed(Date.now() - startedRef.current), 250);
    return () => clearInterval(id);
  }, [running]);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase("idle");
  }, []);

  const start = useCallback((url: string, filename: string) => {
    const controller = new AbortController();
    abortRef.current = controller;
    startedRef.current = Date.now();
    setReceived(0);
    setTotal(0);
    setElapsed(0);
    setError("");
    const remembered = Number(localStorage.getItem(`${DURATION_KEY}.${label}`) || 0);
    setExpected(remembered > 0 ? remembered : null);
    setPhase("preparing");

    void (async () => {
      try {
        const res = await fetch(url, {
          credentials: "include",
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok) {
          let detail = `${res.status} ${res.statusText}`;
          try {
            const body = await res.json();
            if (body && typeof body.detail === "string") detail = body.detail;
          } catch {
            /* the error body was not JSON */
          }
          throw new Error(detail);
        }

        const len = Number(res.headers.get("Content-Length") || 0);
        setTotal(len);
        setPhase("downloading");

        let blob: Blob;
        if (res.body) {
          const reader = res.body.getReader();
          const chunks: BlobPart[] = [];
          let got = 0;
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (value) {
              chunks.push(value as BlobPart);
              got += value.byteLength;
              setReceived(got);
            }
          }
          blob = new Blob(chunks, {
            type: res.headers.get("Content-Type") || "application/octet-stream",
          });
        } else {
          blob = await res.blob();
        }

        setPhase("saving");
        const href = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = href;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Revoked on a tick so the navigation has taken the URL first; revoking synchronously
        // cancels the download in some browsers.
        setTimeout(() => URL.revokeObjectURL(href), 10_000);

        localStorage.setItem(`${DURATION_KEY}.${label}`, String(Date.now() - startedRef.current));
        setPhase("done");
        setTimeout(() => setPhase((p) => (p === "done" ? "idle" : p)), 2500);
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        setError((e as Error)?.message || String(e));
        setPhase("error");
      } finally {
        abortRef.current = null;
      }
    })();
  }, [label]);

  const pct = total > 0 ? Math.min(100, Math.round((received / total) * 100)) : null;
  const dialog =
    phase === "idle" ? null : (
      <div
        className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-progress-title"
      >
        <div className="w-full max-w-md rounded-xl bg-white p-5 shadow-2xl">
          <h2 id="export-progress-title" className="text-sm font-semibold text-gray-900">
            {phase === "error" ? `${label} failed` : phase === "done" ? `${label} ready` : `Preparing ${label}`}
          </h2>

          {phase === "error" ? (
            <p className="mt-2 text-[13px] text-red-700">{error}</p>
          ) : (
            <>
              {/* aria-live so a screen reader hears the phase change rather than only seeing it. */}
              <p className="mt-2 text-[13px] text-gray-600" aria-live="polite">
                {phase === "preparing" && "Building the workbook on the server…"}
                {phase === "downloading" &&
                  (pct === null
                    ? `Downloading… ${fmtBytes(received)}`
                    : `Downloading… ${pct}% of ${fmtBytes(total)}`)}
                {phase === "saving" && "Handing the file to your browser…"}
                {phase === "done" && "Saved. Check your downloads."}
              </p>

              <div className="mt-3 h-1.5 w-full overflow-hidden rounded bg-gray-200">
                {phase === "downloading" && pct !== null ? (
                  <div className="h-full bg-brand transition-[width] duration-200" style={{ width: `${pct}%` }} />
                ) : phase === "done" ? (
                  <div className="h-full w-full bg-green-500" />
                ) : (
                  // No percentage is knowable while the server builds, so the bar says
                  // "working", not "43% complete".
                  <div className="h-full w-1/3 animate-pulse bg-brand" />
                )}
              </div>

              <p className="mt-2 text-[11px] text-gray-500">
                <span className="tabular-nums">{fmtSeconds(elapsed)}</span> elapsed
                {expected
                  ? ` · took about ${fmtSeconds(expected)} last time`
                  : " · no previous run to estimate from"}
              </p>
            </>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={cancel}
              className="rounded border px-3 py-1.5 text-[13px] text-gray-700 hover:bg-gray-50"
            >
              {phase === "error" || phase === "done" ? "Close" : "Cancel"}
            </button>
          </div>
        </div>
      </div>
    );

  return { phase, start, cancel, dialog };
}
