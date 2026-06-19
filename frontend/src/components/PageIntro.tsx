import { useState } from "react";

/**
 * Consistent, dismissible per-page intro: a one-line "what / why" with an optional
 * "Learn more" link. Keeps every major view self-explanatory for new users. Dismissal is
 * remembered per `storageKey` so power users only see it once.
 */
export function PageIntro({
  title,
  blurb,
  learnMoreHref,
  icon = "ℹ️",
  storageKey,
}: {
  title: string;
  blurb: string;
  learnMoreHref?: string;
  icon?: string;
  storageKey?: string;
}) {
  const key = storageKey ? `azsup.intro.dismissed.${storageKey}` : "";
  const [dismissed, setDismissed] = useState(() => (key ? localStorage.getItem(key) === "1" : false));
  if (dismissed) return null;

  function dismiss() {
    if (key) localStorage.setItem(key, "1");
    setDismissed(true);
  }

  return (
    <div className="mb-3 flex items-start gap-3 rounded-lg border border-brand/20 bg-brand/5 px-3 py-2">
      <span className="mt-0.5 text-base" aria-hidden>{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-gray-800">{title}</div>
        <p className="mt-0.5 text-xs text-gray-600">
          {blurb}
          {learnMoreHref && (
            <>
              {" "}
              <a href={learnMoreHref} target="_blank" rel="noreferrer" className="font-medium text-brand hover:underline">
                Learn more →
              </a>
            </>
          )}
        </p>
      </div>
      {storageKey && (
        <button
          onClick={dismiss}
          title="Dismiss"
          aria-label="Dismiss"
          className="shrink-0 rounded p-1 text-gray-400 hover:bg-white hover:text-gray-600"
        >
          ✕
        </button>
      )}
    </div>
  );
}
