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
}) {
  const [expanded, setExpanded] = usePersistedState(`${storageKey}.expanded.v1`, true);
  const bodyId = useId();

  return (
    <section className={className} data-testid={testId}>
      <div className={`flex min-h-8 items-center gap-2 ${headerClassName}`}>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          aria-controls={bodyId}
          title={expanded ? "Collapse history" : "Expand history"}
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