// Real product brand icons for connectors, rendered as inline SVG (no external deps).
// Each icon uses the product's official brand color(s). Falls back to a brand-colored
// monogram tile for any unmapped connector type.
import type { ReactNode } from "react";

function Svg({ children, viewBox = "0 0 24 24" }: { children: ReactNode; viewBox?: string }) {
  return (
    <svg viewBox={viewBox} className="h-full w-full" xmlns="http://www.w3.org/2000/svg">
      {children}
    </svg>
  );
}

// Slack — official 4-colour mark.
const Slack = (
  <Svg>
    <path fill="#36C5F0" d="M9.04 14.5a2.02 2.02 0 1 1-2.02-2.02h2.02v2.02Zm1.01 0a2.02 2.02 0 0 1 4.04 0v5.05a2.02 2.02 0 0 1-4.04 0V14.5Z" />
    <path fill="#2EB67D" d="M12.07 6.46a2.02 2.02 0 1 1 2.02-2.02v2.02h-2.02Zm0 1.02a2.02 2.02 0 0 1 0 4.04H7.02a2.02 2.02 0 0 1 0-4.04h5.05Z" />
    <path fill="#ECB22E" d="M17.54 9.5a2.02 2.02 0 1 1 2.02 2.02h-2.02V9.5Zm-1.01 0a2.02 2.02 0 0 1-4.04 0V4.46a2.02 2.02 0 0 1 4.04 0V9.5Z" />
    <path fill="#E01E5A" d="M14.5 17.54a2.02 2.02 0 1 1-2.02 2.02v-2.02h2.02Zm0-1.01a2.02 2.02 0 0 1 0-4.04h5.05a2.02 2.02 0 0 1 0 4.04H14.5Z" />
  </Svg>
);

// Microsoft Teams — purple rounded square with a white "T".
const Teams = (
  <Svg>
    <circle cx="18.7" cy="5.3" r="2.3" fill="#7B83EB" />
    <rect x="11.2" y="6.6" width="12" height="11" rx="2.2" fill="#5059C9" />
    <rect x="2" y="5" width="13.5" height="14" rx="2.4" fill="#7B83EB" />
    <path fill="#fff" d="M5.1 8.1h7.3v1.7H9.6v6.1H7.9V9.8H5.1V8.1Z" />
  </Svg>
);

// Microsoft Outlook — blue "O" + envelope.
const Outlook = (
  <Svg>
    <rect x="9.5" y="5.5" width="13" height="13" rx="1.4" fill="#0A2767" />
    <path fill="#0364B8" d="M22.5 7v10l-6.5 3.5L9.5 17V7l6.5-3.5L22.5 7Z" opacity="0" />
    <rect x="1.5" y="4" width="12" height="16" rx="2" fill="#0078D4" />
    <text x="7.5" y="16" textAnchor="middle" fontSize="12" fontWeight="700" fill="#fff" fontFamily="Segoe UI, Arial, sans-serif">O</text>
    <path fill="#fff" d="m14 9 4 2.6L22 9v1.4l-4 2.6-4-2.6V9Z" />
  </Svg>
);

// Jira — blue chevron logo (Atlassian).
const Jira = (
  <Svg>
    <path fill="#2684FF" d="M11.6 2.4 5 9l-2.6 2.6a.9.9 0 0 0 0 1.3L11.6 22l2.6-2.6-6.9-6.8 6.9-6.9-2.6-2.3Z" />
    <path fill="#2684FF" d="M12.4 21.6 19 15l2.6-2.6a.9.9 0 0 0 0-1.3L12.4 2l-2.6 2.6 6.9 6.8-6.9 6.9 2.6 2.3Z" opacity=".6" />
  </Svg>
);

// ServiceNow — green doughnut mark.
const ServiceNow = (
  <Svg>
    <circle cx="12" cy="12" r="9.5" fill="#81B5A1" />
    <circle cx="12" cy="12" r="4.2" fill="none" stroke="#fff" strokeWidth="2.4" />
  </Svg>
);

// Grafana — orange.
const Grafana = (
  <Svg>
    <circle cx="12" cy="12" r="9.5" fill="#F46800" />
    <path fill="#fff" d="M12 6.5a5.5 5.5 0 1 0 5.3 7H15a3.2 3.2 0 1 1-.3-3.6l1.9-1.3A5.5 5.5 0 0 0 12 6.5Z" />
  </Svg>
);

// PagerDuty — green stacked rounded bars.
const PagerDuty = (
  <Svg>
    <rect x="3" y="3" width="18" height="18" rx="3" fill="#06AC38" />
    <path fill="#fff" d="M8 6.5h4.6c2.6 0 4.4 1.5 4.4 3.9s-1.8 4-4.4 4H10v3.1H8V6.5Zm2 2v3.8h2.4c1.4 0 2.4-.7 2.4-1.9s-1-1.9-2.4-1.9H10Z" />
  </Svg>
);

// Splunk — green/black ">" arrow.
const Splunk = (
  <Svg>
    <rect x="3" y="3" width="18" height="18" rx="4" fill="#000" />
    <path fill="#65A637" d="M7 7.5 15.5 12 7 16.5v-2.3L11.6 12 7 9.8V7.5Z" />
  </Svg>
);

// Cortex XSOAR (Palo Alto) — teal hexagon.
const Xsoar = (
  <Svg>
    <path fill="#00CC66" d="M12 2.2 20.5 7v10L12 21.8 3.5 17V7L12 2.2Z" />
    <path fill="#fff" d="M12 6.8 16.5 9.5v5L12 17.2 7.5 14.5v-5L12 6.8Zm0 2.3-2.5 1.5v2.8l2.5 1.5 2.5-1.5v-2.8L12 9.1Z" />
  </Svg>
);

// AWS — "aws" wordmark over the signature orange "smile" swoosh.
const Aws = (
  <Svg viewBox="0 0 24 24">
    <text
      x="12"
      y="12.5"
      textAnchor="middle"
      fontFamily="Arial, Helvetica, sans-serif"
      fontWeight="700"
      fontSize="9"
      letterSpacing="0.3"
      fill="#232F3E"
    >
      aws
    </text>
    <path fill="#FF9900" d="M4.6 16.1c2.2 1.6 5 2.4 7.8 2.4 1.9 0 4-.4 5.9-1.2.3-.1.5.2.3.4-1.7 1.5-4.2 2.2-6.4 2.2-3 0-5.8-1.1-7.9-3-.2-.2 0-.4.3-.3Z" />
    <path fill="#FF9900" d="M18.9 15.3c.6-.1 2-.3 2.3.1.2.4-.2 1.7-.6 2.3-.1.2-.3.1-.2-.1.2-.5.6-1.7.4-2-.2-.2-1.5-.1-2-.1-.2 0-.2-.2 0-.2Z" />
  </Svg>
);

// Microsoft Azure — blue "A" (used for Service Bus).
const Azure = (
  <Svg>
    <path fill="#0089D6" d="M8.8 3.3h6L8.2 22h-4.6L8.8 11l-2.6.4L9.7 3.3Z" opacity="0" />
    <path fill="#0078D4" d="M10.3 4h5.3L21 20h-4.4l-3.3-9.8L9.5 17h3l.9 3H4.5L10.3 4Z" />
  </Svg>
);

// Generic webhook — gray link/hook glyph.
const Webhook = (
  <Svg>
    <circle cx="12" cy="12" r="9.5" fill="#6B7280" />
    <path fill="#fff" d="M12 7.2a2.6 2.6 0 0 1 2.5 3.3l1.4 2.3a2.4 2.4 0 1 1-1.2.8l-1.4-2.4-.3.1a2.6 2.6 0 0 1-1.5-.2l-1.5 2.5a2.4 2.4 0 1 1-1.2-.7l1.5-2.6A2.6 2.6 0 0 1 12 7.2Zm0 1.4a1.2 1.2 0 1 0 0 2.4 1.2 1.2 0 0 0 0-2.4Z" />
  </Svg>
);

// Generic email — envelope glyph.
const Email = (
  <Svg>
    <rect x="2.5" y="5" width="19" height="14" rx="2.2" fill="#0F766E" />
    <path fill="none" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" d="M4 7.5l8 5.5 8-5.5" />
  </Svg>
);

const ICONS: Record<string, ReactNode> = {
  slack: Slack,
  teams: Teams,
  outlook: Outlook,
  email: Email,
  jira: Jira,
  servicenow: ServiceNow,
  grafana: Grafana,
  pagerduty: PagerDuty,
  splunk: Splunk,
  xsoar: Xsoar,
  webhook: Webhook,
  sqs: Aws,
  s3: Aws,
  securityhub: Aws,
  servicebus: Azure,
};

// Brand color for the monogram fallback.
const FALLBACK_COLOR: Record<string, string> = {};

function Monogram({ type }: { type: string }) {
  const letter = (type[0] || "?").toUpperCase();
  const color = FALLBACK_COLOR[type] || "#64748B";
  return (
    <Svg>
      <circle cx="12" cy="12" r="11" fill={color} />
      <text x="12" y="16.5" textAnchor="middle" fontSize="12" fontWeight="700" fill="#fff" fontFamily="Inter, Arial, sans-serif">
        {letter}
      </text>
    </Svg>
  );
}

/** Real product brand icon for a connector type. */
export function BrandIcon({ type, className = "h-5 w-5" }: { type: string; className?: string }) {
  const icon = ICONS[type];
  return <span className={`inline-block shrink-0 ${className}`}>{icon ?? <Monogram type={type} />}</span>;
}
