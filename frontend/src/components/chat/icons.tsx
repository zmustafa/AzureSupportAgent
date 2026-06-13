// Pure, dependency-free presentational icons used across ChatView and its
// sub-panels. Extracted from ChatView.tsx to keep that file focused on behavior.


export function ComposeIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M9 4H6a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-3" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M13.4 3.6a1.4 1.4 0 012 2L10 11l-2.8.8.8-2.8 5.4-5.4z" strokeLinejoin="round" />
    </svg>
  );
}

export function SearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="9" cy="9" r="5.5" />
      <path d="M13.5 13.5L17 17" strokeLinecap="round" />
    </svg>
  );
}

export function PinIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor">
      <path d="M12.6 2.3a1 1 0 00-1.5 0L8.3 5.1l-3.1.7a1 1 0 00-.5 1.7l2.2 2.2-3.2 4.4a.6.6 0 00.85.85l4.4-3.2 2.2 2.2a1 1 0 001.7-.5l.7-3.1 2.8-2.8a1 1 0 000-1.5l-2.85-2.85z" />
    </svg>
  );
}

export function PencilIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M13.5 4.5l2 2L7 15l-3 .8.8-3 8.7-8.3z" strokeLinejoin="round" />
    </svg>
  );
}

export function TrashIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M5 6h10M8 6V4.5h4V6M6.5 6l.5 9h6l.5-9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SettingsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="10" cy="10" r="2.5" />
      <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4" strokeLinecap="round" />
    </svg>
  );
}

export function InventoryIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="2.5" y="3" width="15" height="4" rx="1" />
      <rect x="2.5" y="8" width="15" height="4" rx="1" />
      <rect x="2.5" y="13" width="15" height="4" rx="1" />
      <path d="M5 5h.01M5 10h.01M5 15h.01" strokeLinecap="round" />
    </svg>
  );
}

export function WorkloadIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2.5" y="2.5" width="6" height="6" rx="1" />
      <rect x="11.5" y="2.5" width="6" height="6" rx="1" />
      <rect x="2.5" y="11.5" width="6" height="6" rx="1" />
      <rect x="11.5" y="11.5" width="6" height="6" rx="1" />
    </svg>
  );
}

export function DashboardIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 8.5L10 3l7 5.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M4.5 8v8a.5.5 0 00.5.5h3.5V12h3v4.5H15a.5.5 0 00.5-.5V8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function AssessmentIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M7 3.5h6a1.5 1.5 0 011.5 1.5v11A1.5 1.5 0 0113 17.5H7A1.5 1.5 0 015.5 16V5A1.5 1.5 0 017 3.5z" />
      <path d="M8 8.5l1.5 1.5L12 7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8 13h4" strokeLinecap="round" />
    </svg>
  );
}

export function MonitorIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2.5" y="3.5" width="15" height="10" rx="1.5" />      <path d="M7 17h6M10 13.5V17" strokeLinecap="round" />
      <path d="M5.5 10l2-2.5 2 2 2.5-3 2.5 3.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function StatsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 17V3" strokeLinecap="round" />
      <path d="M3 17h14" strokeLinecap="round" />
      <rect x="5.5" y="10" width="2.5" height="5" rx="0.5" />
      <rect x="9.5" y="6.5" width="2.5" height="8.5" rx="0.5" />
      <rect x="13.5" y="8.5" width="2.5" height="6.5" rx="0.5" />
    </svg>
  );
}

export function ArchitectureIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2.5" y="2.5" width="6" height="5" rx="1" />
      <rect x="11.5" y="2.5" width="6" height="5" rx="1" />
      <rect x="7" y="12.5" width="6" height="5" rx="1" />
      <path d="M5.5 7.5v2.5h9V7.5M10 10v2.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function PolicyIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 2.5l5.5 2v4.2c0 3.4-2.3 6.4-5.5 7.3-3.2-.9-5.5-3.9-5.5-7.3V4.5L10 2.5z" strokeLinejoin="round" />
      <path d="M7.5 9.8l1.8 1.8 3.2-3.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IdentityIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 2.5l5.5 2v4.2c0 3.4-2.3 6.4-5.5 7.3-3.2-.9-5.5-3.9-5.5-7.3V4.5L10 2.5z" strokeLinejoin="round" />
      <circle cx="10" cy="8.2" r="1.7" />
      <path d="M10 9.9v3.1M10 11.7h1.6" strokeLinecap="round" />
    </svg>
  );
}

export function CoverageIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 3a7 7 0 107 7" strokeLinecap="round" />
      <path d="M10 3v7l5 2.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M14.3 4.2l1.3 1.3 2.2-2.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function TelemetryIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M2.5 12h3l2-6 3 12 2-7 1.4 3H17.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function BackupIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 2.5l5.5 2v4.2c0 3.4-2.3 6.4-5.5 7.3-3.2-.9-5.5-3.9-5.5-7.3V4.5L10 2.5z" strokeLinejoin="round" />
      <path d="M12.2 8.2a2.4 2.4 0 10-.5 2.6" strokeLinecap="round" />
      <path d="M12.4 6.2v2.1h-2.1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function EvidenceIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3.5" y="3" width="13" height="14" rx="1.5" />
      <path d="M3.5 7h13" strokeLinecap="round" />
      <circle cx="10" cy="11.5" r="1.6" />
      <path d="M10 13.1V15" strokeLinecap="round" />
    </svg>
  );
}

export function RadarIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="10" cy="10" r="7.5" />
      <circle cx="10" cy="10" r="4" />
      <path d="M10 10l5.3-5.3" strokeLinecap="round" />
      <circle cx="10" cy="10" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function TelemetryIntelIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 16V4" strokeLinecap="round" />
      <path d="M3 16h14" strokeLinecap="round" />
      <path d="M5.5 13l3-4 2.5 2.5L16 5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="16" cy="5" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function PerformanceIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 17a7 7 0 1 1 7-7" strokeLinecap="round" />
      <path d="M10 10l3.5-2.5" strokeLinecap="round" />
      <circle cx="10" cy="10" r="1.3" fill="currentColor" stroke="none" />
      <path d="M4 10h1.5M14.5 10H16M10 4v1.5" strokeLinecap="round" />
    </svg>
  );
}

export function ProactiveIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 2.5l5.5 2v4.4c0 3.4-2.3 6.5-5.5 7.6-3.2-1.1-5.5-4.2-5.5-7.6V4.5L10 2.5z" strokeLinejoin="round" />
      <path d="M7.3 9.8l1.9 1.9 3.5-3.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function BoltIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor">
      <path d="M11 1.5L3.5 11H9l-1 7.5L16.5 9H11l0-7.5z" />
    </svg>
  );
}

export function RobotIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="3.5" y="6.5" width="13" height="9" rx="2.5" />
      <path d="M10 3v3.5M7 11h.01M13 11h.01M7.5 14h5" strokeLinecap="round" />
    </svg>
  );
}

export function ChevronRightIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M7.5 5l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SparkleIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor">
      <path d="M10 1.5l1.6 4.4 4.4 1.6-4.4 1.6L10 13.5 8.4 9.1 4 7.5l4.4-1.6L10 1.5zM4.5 12.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2L1.5 15.5l2.2-.8.8-2.2z" />
    </svg>
  );
}

export function PanelLeftIcon({ className, collapsed }: { className?: string; collapsed?: boolean }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="3" y="4" width="14" height="12" rx="1.5" />
      <path d="M8 4v12" />
      <path
        d={collapsed ? "M11.5 8l2 2-2 2" : "M5.5 8l-1.5 2 1.5 2"}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** War Room badge — a Spartan helmet (gold) with red crest and crossed spears inside a
 *  gold-ringed dark medallion. Used to brand the deep-investigation "war room": shown
 *  when launching one and beside deep-investigation chat titles in the sidebar. Rendered
 *  as a self-contained, multi-colour inline SVG (ignores currentColor). */
export function WarRoomIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="War Room"
      role="img"
    >
      {/* Medallion */}
      <circle cx="32" cy="32" r="31" fill="#1e2228" stroke="#C9A24B" strokeWidth="2.5" />
      <circle cx="32" cy="32" r="25.5" fill="none" stroke="#C9A24B" strokeWidth="1" opacity="0.45" />
      {/* Crossed spears (behind the helmet) */}
      <g stroke="#C9A24B" strokeWidth="2.2" strokeLinecap="round">
        <line x1="15" y1="18" x2="49" y2="46" />
        <line x1="49" y1="18" x2="15" y2="46" />
      </g>
      <path d="M15 18 l4.5 0.4 -1.6 4.2 z" fill="#C9A24B" />
      <path d="M49 18 l-4.5 0.4 1.6 4.2 z" fill="#C9A24B" />
      {/* Mask so the spears don't show through the helmet's eye gaps */}
      <ellipse cx="32" cy="34" rx="14.5" ry="17.5" fill="#1e2228" />
      {/* Red crest / plume */}
      <path
        d="M22 18 C21 9 27 6 33 6 C42 6 47 11 46 18 C42 14 37 13 33 14 C28 13 25 15 22 18 Z"
        fill="#9E2B25"
      />
      {/* Corinthian (front-facing) Spartan helmet */}
      <path
        d="M32 16 C40 16 46 22 46 31 C46 36 44.5 40.5 42 43.5 L38.5 40.5 C40.5 38 41.5 34.5 41.5 31 L36 31 L36 45 L32 48 L28 45 L28 31 L22.5 31 C22.5 34.5 23.5 38 25.5 40.5 L22 43.5 C19.5 40.5 18 36 18 31 C18 22 24 16 32 16 Z"
        fill="#C9A24B"
        stroke="#8a6d2e"
        strokeWidth="0.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** A small spinning indicator shown while a step is in progress. */
export function Spinner({ className = "h-3.5 w-3.5 text-brand" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" aria-label="Working">
      <circle className="opacity-20" cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" />
      <path
        className="opacity-90"
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}
