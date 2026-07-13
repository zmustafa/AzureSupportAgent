import { useEffect, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { documentationForPath } from "../help/docsRegistry";

/**
 * Adds a consistent documentation link to the owning screen's first H1. Most screens predate
 * ScreenHeader and implement their heading independently; using a scoped portal gives every
 * route contextual help without duplicating link markup across dozens of feature components.
 */
export function ContextDocumentationHelp({ rootRef, pathname }: { rootRef: RefObject<HTMLElement>; pathname: string }) {
  const [heading, setHeading] = useState<HTMLElement | null>(null);
  const docs = documentationForPath(pathname);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !docs) { setHeading(null); return; }
    const find = () => setHeading(
      root.querySelector<HTMLElement>("main h1")
      ?? root.querySelector<HTMLElement>("main h2")
      ?? root.querySelector<HTMLElement>("h1, h2"),
    );
    find();
    const observer = new MutationObserver(find);
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [docs, pathname, rootRef]);

  if (!docs || !heading) return null;
  return createPortal(
    <span className="ml-1 inline-flex shrink-0 items-center gap-1 align-middle text-[11px] font-medium leading-4">
      <a
        href={docs.guide}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`Open ${docs.label} documentation`}
        title={`Feature guide for ${docs.label}`}
        className="inline-flex items-center gap-1 rounded-md border border-brand/25 bg-white px-1.5 py-0.5 text-brand no-underline shadow-sm hover:bg-brand/5 focus:outline-none focus:ring-2 focus:ring-brand/30"
      >
        <span aria-hidden>?</span><span>Help</span><span aria-hidden className="text-[9px]">↗</span>
      </a>
      {docs.howTo && (
        <a
          href={docs.howTo}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${docs.label} how-to guide`}
          title={`How-to guide for ${docs.label}`}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-gray-500 no-underline hover:bg-gray-100 hover:text-brand focus:outline-none focus:ring-2 focus:ring-brand/30"
        >
          <span>How-to</span><span aria-hidden className="text-[9px]">↗</span>
        </a>
      )}
    </span>,
    heading,
  );
}
