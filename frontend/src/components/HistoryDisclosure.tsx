import { useId, type ReactNode } from "react";
import { usePersistedState } from "../utils/persistedState";
import { ChevronRightIcon } from "./chat/icons";

export function HistoryDisclosure({
  storageKey,
  title,
  count,
  headerExtra,
  actions,
  children,
  className = "",
  headerClassName = "",
  bodyClassName = "",
  testId,
  label = "history",
  defaultExpanded = true,
}: {
  storageKey: string;
  title: ReactNode;
  count?: ReactNode;
  headerExtra?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  headerClassName?: string;
  bodyClassName?: string;
  testId?: string;
  /** Names the thing being collapsed in the tooltip. Defaults to "history" for the run-history
   *  panels this started life as. */
  label?: string;
  defaultExpanded?: boolean;
}) {
  const [expanded, setExpanded] = usePersistedState(`${storageKey}.expanded.v1`, defaultExpanded);
  const bodyId = useId();

  return (
    <section className={className} data-testid={testId}>
      <div className={`flex min-h-8 items-center gap-2 ${headerClassName}`}>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          aria-controls={bodyId}
          title={expanded ? `Collapse ${label}` : `Expand ${label}`}
          className="flex min-w-0 items-center gap-2 text-left"
        >
          <ChevronRightIcon className={`h-3.5 w-3.5 shrink-0 text-gray-400 transition-transform ${expanded ? "rotate-90" : ""}`} />
          <span className="min-w-0">{title}</span>
        </button>
        {count}
        {headerExtra}
        <div className="ml-auto flex shrink-0 items-center gap-2" onClick={() => { if (!expanded) setExpanded(true); }}>
          {actions}
        </div>
      </div>
      <div id={bodyId} hidden={!expanded} className={bodyClassName}>
        {children}
      </div>
    </section>
  );
}