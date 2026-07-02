import type { ReactNode } from "react";

export type StatusTone = "gray" | "brand" | "green" | "amber" | "red" | "blue";

const TONE: Record<StatusTone, string> = {
  gray: "bg-gray-900/5 text-gray-500",
  brand: "bg-brand/10 text-brand",
  green: "bg-green-100 text-green-700",
  amber: "bg-amber-100 text-amber-700",
  red: "bg-red-100 text-red-700",
  blue: "bg-blue-100 text-blue-700",
};

/** Standard status pill used in screen headers (uppercase micro-label). */
export function StatusPill({ label, tone = "gray" }: { label: string; tone?: StatusTone }) {
  return (
    <span
      className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${TONE[tone]}`}
    >
      {label}
    </span>
  );
}

/**
 * Canonical page header for every top-level screen. One title / icon / subtitle language so the
 * app feels unified — fixes the drift where some screens use emoji-in-title, some use SVG chips,
 * some none, and status pills vary ("OPERATIONS" / "read-only" / "Governance toolkit").
 *
 * - `icon`    emoji string or an SVG node, rendered in a consistent leading slot.
 * - `status`  optional micro-pill (e.g. "Read-only", "Operations").
 * - `actions` right-aligned controls (scope pickers, refresh, export…).
 * - `footer`  optional secondary row rendered inside the header container (tabs, status line…).
 */
export function ScreenHeader({
  icon,
  title,
  subtitle,
  status,
  actions,
  footer,
  className = "",
}: {
  icon?: ReactNode;
  title: string;
  subtitle?: ReactNode;
  status?: { label: string; tone?: StatusTone };
  actions?: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`border-b bg-white px-5 py-3 ${className}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
            {icon != null && (
              <span aria-hidden className="flex items-center text-[1.1em] leading-none">
                {icon}
              </span>
            )}
            <span className="truncate">{title}</span>
            {status && <StatusPill label={status.label} tone={status.tone} />}
          </h1>
          {subtitle && <p className="mt-0.5 max-w-3xl text-[13px] text-gray-500">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {footer}
    </div>
  );
}
