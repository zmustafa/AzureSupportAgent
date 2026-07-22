import { documentationForPath } from "../help/docsRegistry";

/**
 * Declarative contextual documentation links for the current route.
 *
 * This deliberately renders in the stable application header instead of portaling into a
 * feature-owned heading. A portal made an arbitrary h1/h2 contain children from two React
 * subtrees; when a screen replaced a text-only heading after an async load, React's textContent
 * update removed the portal DOM behind the portal fiber and the next navigation crashed while
 * trying to remove that already-detached node.
 */
export function ContextDocumentationHelp({ pathname }: { pathname: string }) {
  const docs = documentationForPath(pathname);

  if (!docs) return null;
  return (
    <span className="hidden shrink-0 items-center gap-1 text-[11px] font-medium lg:inline-flex">
      <a
        href={docs.guide}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`Open ${docs.label} documentation`}
        title={`Feature guide for ${docs.label}`}
        className="inline-flex items-center gap-1 rounded-md border border-white/20 bg-white/10 px-1.5 py-0.5 text-white/80 no-underline hover:bg-white/20 hover:text-white focus:outline-none focus:ring-2 focus:ring-white/40"
      >
        <span aria-hidden>?</span><span>{docs.label}</span><span aria-hidden className="text-[9px]">↗</span>
      </a>
      {docs.howTo && (
        <a
          href={docs.howTo}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${docs.label} how-to guide`}
          title={`How-to guide for ${docs.label}`}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-white/60 no-underline hover:bg-white/10 hover:text-white focus:outline-none focus:ring-2 focus:ring-white/40"
        >
          <span>How-to</span><span aria-hidden className="text-[9px]">↗</span>
        </a>
      )}
    </span>
  );
}
