import { useEffect, useRef, useState } from "react";

/**
 * Copy-to-clipboard with a transient "copied" confirmation. The reset timeout is
 * cleared on unmount so it never calls setState on an unmounted component.
 */
export function useCopy(): [boolean, (text: string) => Promise<void>] {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  return [copied, copy];
}

const CheckIcon = ({ className }: { className?: string }) => (
  <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M5 10l3.5 3.5L15 6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const ClipboardIcon = ({ className }: { className?: string }) => (
  <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="7" y="7" width="9" height="9" rx="1.5" />
    <path d="M4 13V4.5A1.5 1.5 0 015.5 3H13" strokeLinecap="round" />
  </svg>
);

type CopyButtonProps = {
  /** Text to copy, or a getter (deferred for content not available until click). */
  content: string | (() => string);
  className?: string;
  title?: string;
  /** Show a text label next to the icon. */
  label?: string;
  /** Override the icon color classes. */
  checkClassName?: string;
  iconClassName?: string;
};

/** A single, reusable copy control used by messages, answers, and code blocks. */
export function CopyButton({
  content,
  className,
  title = "Copy",
  label,
  checkClassName = "h-3.5 w-3.5 text-green-600",
  iconClassName = "h-3.5 w-3.5",
}: CopyButtonProps) {
  const [copied, copy] = useCopy();
  return (
    <button
      onClick={() => void copy(typeof content === "function" ? content() : content)}
      className={className}
      title={title}
    >
      {copied ? <CheckIcon className={checkClassName} /> : <ClipboardIcon className={iconClassName} />}
      {label != null && <span>{copied ? "Copied" : label}</span>}
    </button>
  );
}
