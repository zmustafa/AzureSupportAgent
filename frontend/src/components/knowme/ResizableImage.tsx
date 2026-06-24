// Resizable image node for the Know-Me TipTap editor. Extends the base Image with a React
// node view that draws a drag handle when selected; dragging sets the display width. The
// width is stored as a ``?w=<px>`` query param ON THE SRC (not a separate attribute) so it
// round-trips through standard Markdown (`![alt](src?w=420)`) with no custom serializer —
// the document read view and PDF export both understand the same hint.
import Image from "@tiptap/extension-image";
import { NodeViewWrapper, ReactNodeViewRenderer } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import { useRef } from "react";

export function widthFromSrc(src: string | null | undefined): number | null {
  const m = /[?&]w=(\d+)/.exec(src || "");
  return m ? parseInt(m[1], 10) : null;
}

export function setWidthInSrc(src: string | null | undefined, w: number | null): string {
  const base = (src || "").replace(/[?&]w=\d+/g, "").replace(/\?$/, "");
  if (w == null) return base;
  return base + (base.includes("?") ? "&" : "?") + "w=" + w;
}

function ResizableImageView({ node, updateAttributes, selected, editor }: NodeViewProps) {
  const imgRef = useRef<HTMLImageElement>(null);
  const src = node.attrs.src as string;
  const width = widthFromSrc(src);
  // Display from the width-less URL so the browser caches ONE image — otherwise every drag
  // step changes the src query and triggers a fresh (aborted) fetch. The width lives only in
  // the node's stored src (for Markdown round-trip) and is applied via style here.
  const displaySrc = setWidthInSrc(src, null);

  function startResize(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = imgRef.current?.offsetWidth ?? 200;
    const onMove = (ev: MouseEvent) => {
      const next = Math.max(60, Math.round(startW + (ev.clientX - startX)));
      updateAttributes({ src: setWidthInSrc(src, next) });
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  const editable = editor.isEditable;
  return (
    <NodeViewWrapper className="km-img-wrap" style={{ display: "inline-block", position: "relative", lineHeight: 0, maxWidth: "100%" }}>
      <img
        ref={imgRef}
        src={displaySrc}
        alt={(node.attrs.alt as string) || ""}
        draggable={false}
        style={{ width: width ? `${width}px` : "auto", maxWidth: "100%", borderRadius: 8, display: "block" }}
        className={selected ? "ring-2 ring-brand" : ""}
      />
      {editable && selected && (
        <>
          <span
            onMouseDown={startResize}
            title="Drag to resize"
            style={{ position: "absolute", right: -5, bottom: -5, width: 14, height: 14, background: "#fff", border: "2px solid var(--brand,#6366f1)", borderRadius: 4, cursor: "nwse-resize", boxShadow: "0 1px 3px rgba(0,0,0,.2)" }}
          />
          {width && (
            <span style={{ position: "absolute", left: 4, bottom: 4, background: "rgba(0,0,0,.6)", color: "#fff", fontSize: 10, padding: "1px 5px", borderRadius: 4 }}>
              {width}px
            </span>
          )}
        </>
      )}
    </NodeViewWrapper>
  );
}

export const ResizableImage = Image.extend({
  // Keep the default Image schema (src/alt/title) — width lives in the src query param so it
  // serializes through Markdown for free. Just swap in the React node view with the handle.
  addNodeView() {
    return ReactNodeViewRenderer(ResizableImageView);
  },
});
