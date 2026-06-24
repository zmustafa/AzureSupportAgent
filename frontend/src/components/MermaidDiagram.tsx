// Shared Mermaid diagram renderer — extracted so the chat, the Know-Me document and any
// other markdown surface render diagrams identically (lazy mermaid import, strict security,
// DOMPurify SVG sanitize, source/preview/fullscreen toolbar, flicker-free re-render).
import { memo, useEffect, useRef, useState } from "react";
import { Spinner } from "./chat/icons";

export const MermaidDiagram = memo(function MermaidDiagram({ code }: { code: string }) {
  const { svg, error } = useMermaidRender(code);
  const [showSource, setShowSource] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  // A single layout for every state (diagram / source / error / loading) so the
  // toolbar is ALWAYS present — the user can never get stranded in a button-less
  // source/error view.
  const failed = !!error && !svg;
  const sourceShown = failed || showSource;

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  const tabBtn = (active: boolean) =>
    `inline-flex h-7 w-7 items-center justify-center rounded-md border text-gray-600 transition ${
      active
        ? "border-gray-300 bg-white text-gray-900 shadow-sm"
        : "border-transparent hover:border-gray-200 hover:bg-white/70"
    }`;

  return (
    <div
      className={`my-2 overflow-hidden rounded-lg border bg-white ${
        failed ? "border-amber-300" : "border-gray-200"
      }`}
    >
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-xs font-medium text-gray-600">
          <svg className="h-3.5 w-3.5 text-gray-500" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <rect x="3" y="3" width="6" height="5" rx="1" />
            <rect x="11" y="12" width="6" height="5" rx="1" />
            <path d="M6 8v3a1 1 0 001 1h7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Mermaid
        </div>
        <div className="flex items-center gap-1">
          <button onClick={() => setShowSource(true)} disabled={failed} title="Code" className={tabBtn(sourceShown)}>
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M8 6l-4 4 4 4M12 6l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button onClick={() => setShowSource(false)} disabled={failed} title="Preview" className={tabBtn(!sourceShown)}>
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6 4.5l9 5.5-9 5.5z" />
            </svg>
          </button>
          <button onClick={() => setFullscreen(true)} disabled={failed || !svg} title="Full screen" className={tabBtn(false)}>
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M8 3H3v5M12 3h5v5M8 17H3v-5M12 17h5v-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>
      {failed ? (
        <>
          <div className="border-b border-amber-200 px-3 py-1.5 text-[11px] text-amber-700">
            Couldn&rsquo;t render Mermaid diagram — showing source
          </div>
          <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-amber-900">{code}</pre>
        </>
      ) : sourceShown ? (
        <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-gray-700">{code}</pre>
      ) : svg ? (
        <div
          className="mermaid-diagram flex justify-center overflow-x-auto px-3 py-4 [&_svg]:h-auto [&_svg]:max-w-full"
          // svg comes from useMermaidRender which renders via Mermaid's securityLevel:"strict"
          // parser and runs the result through DOMPurify's SVG profile (defense-in-depth).
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <div className="flex items-center gap-2 px-3 py-6 text-xs text-gray-400">
          <Spinner className="h-3.5 w-3.5 text-gray-400" />
          Rendering diagram…
        </div>
      )}

      {fullscreen && svg && (
        <div className="fixed inset-0 z-50 flex flex-col bg-black/60 backdrop-blur-sm" onClick={() => setFullscreen(false)}>
          <div className="flex items-center justify-between px-4 py-2 text-white">
            <span className="text-sm font-medium">Mermaid</span>
            <button onClick={() => setFullscreen(false)} title="Close (Esc)" className="inline-flex h-8 w-8 items-center justify-center rounded-md text-white/80 transition hover:bg-white/20 hover:text-white">
              <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
              </svg>
            </button>
          </div>
          <div
            className="mermaid-diagram m-4 mt-0 flex flex-1 items-center justify-center overflow-auto rounded-lg bg-white p-6 [&_svg]:h-auto [&_svg]:max-h-full [&_svg]:max-w-full"
            onClick={(e) => e.stopPropagation()}
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
      )}
    </div>
  );
});

/** Renders Mermaid `code` to an SVG string, returning `{ svg, error }`. Mermaid is
 * imported lazily on first use. Shared by the chat, the Mermaid editor preview and the
 * Know-Me document so they stay visually identical. */
export function useMermaidRender(code: string): { svg: string; error: string } {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  // Keep the latest successfully-rendered source so we can suppress redundant
  // re-renders (e.g. identical code after a parent re-render) without flicker.
  const lastRenderedRef = useRef<string>("");

  useEffect(() => {
    const trimmed = code.trim();
    if (!trimmed) {
      setSvg("");
      setError("");
      lastRenderedRef.current = "";
      return;
    }
    if (trimmed === lastRenderedRef.current && svg) return;

    let cancelled = false;
    // Debounce so streaming/typing coalesces into one render once the source settles.
    const timer = window.setTimeout(() => {
      const renderId = `mmd-${Math.random().toString(36).slice(2)}`;
      (async () => {
        try {
          const mermaid = (await import("mermaid")).default;
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict", // sanitize labels/links — never inject raw HTML
            theme: "neutral",
            fontFamily: "inherit",
            // Native SVG <text> labels (not HTML <foreignObject>) survive the SVG-profile
            // DOMPurify sanitize below intact.
            htmlLabels: false,
            flowchart: { htmlLabels: false },
          });
          const { svg: out } = await mermaid.render(renderId, trimmed);
          const DOMPurify = (await import("dompurify")).default;
          const safeSvg = DOMPurify.sanitize(out, { USE_PROFILES: { svg: true, svgFilters: true } });
          if (!cancelled) {
            setSvg(safeSvg);
            setError("");
            lastRenderedRef.current = trimmed;
          }
        } catch (e) {
          if (!cancelled) setError((e as Error)?.message ?? "Failed to render diagram.");
        } finally {
          // mermaid can leave an orphan measurement node in <body> on interrupted renders.
          document.getElementById(renderId)?.remove();
          document.getElementById(`d${renderId}`)?.remove();
        }
      })();
    }, 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  return { svg, error };
}
