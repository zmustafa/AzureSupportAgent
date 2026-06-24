// Know-Me human-completion field helpers — typed inputs, context extraction, validation.
import type { KnowMeTodo } from "../../api";

export const TODO_RE = /⟦TODO:\s*([^⟧|]+?)(?:\s*\|\s*key=([a-z0-9_]+))?(?:\s*\|\s*choices=[^⟧]+?)?\s*⟧/g;

export const FIELD_META: Record<KnowMeTodo["type"], { icon: string; label: string; placeholder: string }> = {
  email: { icon: "✉️", label: "Email", placeholder: "name@company.com" },
  person: { icon: "👤", label: "Person", placeholder: "Full name or email" },
  group: { icon: "👥", label: "Group / team", placeholder: "On-call group / distribution list" },
  duration: { icon: "⏱️", label: "Duration / window", placeholder: "e.g. 4h, 24×7, Mon–Fri 9–5 UTC" },
  datetime: { icon: "📅", label: "Date", placeholder: "YYYY-MM-DD" },
  number: { icon: "🔢", label: "Threshold / number", placeholder: "e.g. 80%, 240 IOPS" },
  url: { icon: "🔗", label: "URL", placeholder: "https://…" },
  text: { icon: "📝", label: "Text", placeholder: "Enter value…" },
};

export const GROUP_LABELS: Record<string, string> = {
  escalation: "Escalation & on-call",
  contacts: "Contacts",
  resiliency: "Resiliency (RTO / RPO)",
  sla: "SLAs / SLOs",
  thresholds: "Thresholds & SLIs",
  contract: "Contract & schedule",
  scope: "Scope details",
  network: "Network & connectivity",
  identity: "Identity & access",
  ownership: "Ownership",
  links: "Links",
  other: "Other",
};

export function groupLabel(g: string): string {
  return GROUP_LABELS[g] ?? (g ? g[0].toUpperCase() + g.slice(1) : "Other");
}

// Frontend fallback group classifier — mirrors the backend rules so that documents
// generated before the rules were extended still group network/identity/scope fields
// sensibly instead of dumping them all into "Other".
const _GROUP_FALLBACK: [RegExp, string][] = [
  [/on[_ -]?call|oncall|escalation|coverage[_ ]?(window|hours)|assignment[_ ]?group|support[_ ]?group|duty[_ ]?manager/, "escalation"],
  [/\brto\b|\brpo\b|recovery[_ ]?(time|point)/, "resiliency"],
  [/\bsla\b|\bslo\b|response[_ ]?time|resolution[_ ]?time/, "sla"],
  [/threshold|target[_ ]?value|breach[_ ]?at|warn[_ ]?at|alert[_ ]?at|sli[_ ]?target/, "thresholds"],
  [/contract|schedule[_ ]?id|agreement[_ ]?id|po[_ ]?number|case[_ ]?number|ticket|\bdate\b|expiry|expires|renewal|effective/, "contract"],
  [/vnet|virtual[_ ]?network|subnet|cidr|address[_ ]?space|private[_ ]?endpoint|private[_ ]?link|\bnsg\b|firewall|\bip\b|ip[_ ]?address|dns|fqdn|peering|gateway|route[_ ]?table|egress|ingress/, "network"],
  [/tenant|principal|managed[_ ]?identity|\bmsi\b|service[_ ]?principal|\brbac\b|role[_ ]?assignment|app[_ ]?registration|object[_ ]?id|client[_ ]?id/, "identity"],
  [/region|location|friendly[_ ]?name|display[_ ]?name|sub(scription)?[_ ]?name|resource[_ ]?group|\brg\b|\bsku\b|tier|capacity|instance[_ ]?count/, "scope"],
  [/url|link|portal|dashboard|runbook[_ ]?url|wiki|repo|pipeline/, "links"],
  [/cost[_ ]?cent(er|re)|department|business[_ ]?unit|\bowner\b|budget|charge[_ ]?back/, "ownership"],
  [/contact|account[_ ]?manager|customer|stakeholder/, "contacts"],
];

/** The display group for a field — uses the stored group, but re-derives a better one when
 *  the stored group is the catch-all "other" (handles pre-existing docs). */
export function effectiveGroup(todo: KnowMeTodo): string {
  if (todo.group && todo.group !== "other") return todo.group;
  const blob = `${todo.field_key} ${todo.label}`.toLowerCase();
  for (const [re, g] of _GROUP_FALLBACK) if (re.test(blob)) return g;
  return "other";
}

// ---------------------------------------------------------------- content cleaning
const GENERIC_NODE_WORDS = new Set([
  "client", "clients", "user", "users", "browser", "service", "services", "api", "apis",
  "app", "apps", "application", "frontend", "front", "backend", "back", "server", "servers",
  "database", "databases", "db", "datastore", "store", "cache", "queue", "gateway",
  "loadbalancer", "lb", "internet", "cloud", "system",
]);
const MERMAID_NODE_RE = /[[({>]+\s*"?([^"\])}>|]+?)"?\s*[\])}]+/g;

/** True for the trivial stock diagram (all-generic node labels) a model emits when it has
 *  no real topology — we never render these. */
export function isPlaceholderMermaid(code: string): boolean {
  const body = (code || "").trim();
  if (!body) return true;
  const cleaned: string[] = [];
  let m: RegExpExecArray | null;
  MERMAID_NODE_RE.lastIndex = 0;
  while ((m = MERMAID_NODE_RE.exec(body))) {
    const head = (m[1] || "").split(/<br\s*\/?>/i)[0];
    const n = head.toLowerCase().replace(/[^a-z0-9]+/g, "");
    if (n) cleaned.push(n);
  }
  if (!cleaned.length) return false;
  return cleaned.length <= 6 && cleaned.every((n) => [...GENERIC_NODE_WORDS].some((w) => w === n || (n.length > 3 && n.includes(w))));
}

const HEADING_RE = /^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$/;
function norm(s: string): string {
  return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** Drop a leading Markdown heading that just repeats the section label, and strip any
 *  generic placeholder Mermaid diagram. Mirrors the backend ``clean_section_content``. */
export function cleanSectionContent(content: string, label: string): string {
  let out = content || "";
  // strip placeholder mermaid fences
  if (/```mermaid/i.test(out)) {
    out = out.replace(/```mermaid\s*\n([\s\S]*?)```/gi, (full, body) => (isPlaceholderMermaid(body) ? "" : full));
  }
  // strip a redundant leading heading
  const lines = out.split("\n");
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i++;
  if (i < lines.length) {
    const hm = HEADING_RE.exec(lines[i].trim());
    if (hm) {
      const heading = norm(hm[1]);
      const lbl = norm(label);
      if (lbl && (heading === lbl || heading.startsWith(lbl))) {
        const rest = lines.slice(i + 1);
        while (rest.length && !rest[0].trim()) rest.shift();
        out = [...lines.slice(0, i), ...rest].join("\n").trim();
      }
    }
  }
  return out;
}

/** HTML input type for a field type (used by the typed input control). */
export function htmlInputType(t: KnowMeTodo["type"]): string {
  switch (t) {
    case "email":
      return "email";
    case "datetime":
      return "date";
    case "number":
      return "text"; // numbers often carry units (80%, 240 IOPS) → free text
    case "url":
      return "url";
    default:
      return "text";
  }
}

/** Validate a value for a field type. Returns an error string, or "" when valid. */
export function validateField(t: KnowMeTodo, value: string): string {
  const v = (value || "").trim();
  if (!v) return t.required ? "Required" : "";
  // Strict choice set: the value must be one of the offered options.
  if (t.choices?.length && t.allow_custom === false && !t.choices.includes(v)) {
    return "Pick one of the options";
  }
  if (t.type === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return "Enter a valid email";
  if (t.type === "url" && !/^https?:\/\/\S+$/i.test(v)) return "Enter a valid URL (https://…)";
  if (t.type === "datetime" && !/^\d{4}-\d{2}-\d{2}/.test(v)) return "Use YYYY-MM-DD";
  return "";
}

/** The control to render for a field: a segmented button row for a small strict set, a
 *  combobox (typeahead + free text) for a picker, a yes/no toggle, else the typed input. */
export function fieldControl(t: KnowMeTodo): "segmented" | "combobox" | "input" {
  const n = t.choices?.length ?? 0;
  if (!n) return "input";
  // Strict, small sets (Yes/No, Critical/High/Medium/Low) → segmented buttons.
  if (t.allow_custom === false && n <= 5) return "segmented";
  return "combobox";
}

const SEVERITY_PALETTE: Record<string, string> = {
  critical: "border-red-300 bg-red-50 text-red-700",
  high: "border-orange-300 bg-orange-50 text-orange-700",
  medium: "border-amber-300 bg-amber-50 text-amber-700",
  low: "border-emerald-300 bg-emerald-50 text-emerald-700",
  yes: "border-emerald-300 bg-emerald-50 text-emerald-700",
  no: "border-rose-300 bg-rose-50 text-rose-700",
  unknown: "border-gray-300 bg-gray-50 text-gray-600",
  "n/a": "border-gray-300 bg-gray-50 text-gray-500",
};

/** Selected-state classes for a segmented option, color-coded for severity / yes-no. */
export function optionAccent(option: string): string {
  return SEVERITY_PALETTE[option.trim().toLowerCase()] ?? "border-brand bg-brand/10 text-brand";
}

/** Order todos by document position (section order, then appearance) for guided fill. */
export function orderTodos(todos: KnowMeTodo[], sectionKeys: string[]): KnowMeTodo[] {
  const idx = new Map(sectionKeys.map((k, i) => [k, i]));
  return [...todos].sort((a, b) => (idx.get(a.section_key) ?? 99) - (idx.get(b.section_key) ?? 99));
}

/** Extract a short plain-text context snippet around a field's token within its section. */
export function fieldContext(sectionContent: string, todo: KnowMeTodo): string {
  const content = sectionContent || "";
  const re = new RegExp(TODO_RE.source, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(content))) {
    const label = (m[1] || "").trim();
    const key = (m[2] || "").trim();
    const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
    const tid = `${todo.section_key}:${key || slug}`;
    if (tid === todo.id) {
      const start = Math.max(0, m.index - 90);
      const end = Math.min(content.length, m.index + m[0].length + 90);
      let snip = content.slice(start, end).replace(TODO_RE, "▮▮▮").replace(/\s+/g, " ").trim();
      // strip markdown table pipes / heading hashes for readability
      snip = snip.replace(/^#+\s*/, "").replace(/\|/g, " · ");
      return (start > 0 ? "…" : "") + snip + (end < content.length ? "…" : "");
    }
  }
  return "";
}

export interface FieldProgress {
  total: number;
  done: number;
  open: number;
  requiredOpen: number;
}

export function fieldProgress(todos: KnowMeTodo[]): FieldProgress {
  const total = todos.length;
  const done = todos.filter((t) => t.status === "done").length;
  const requiredOpen = todos.filter((t) => t.required && t.status !== "done").length;
  return { total, done, open: total - done, requiredOpen };
}

/** Render a section's raw markdown for the READ view: substitute filled ⟦TODO⟧ values
 *  inline, show open ones as a highlighted marker. Mirrors the backend overlay so the
 *  document reads naturally. ``highlightId`` flashes the current guided-fill field. */
// A fillable ⟦TODO⟧ field is rendered (inside an inline-code span) as an opaque token the
// document's Markdown ``code`` renderer turns into a clickable, bordered field chip. The
// token carries the field's id, label, and (when filled) its value — so a filled field stays
// an editable chip showing its value, not inert text. ``\u241F`` (INFORMATION SEPARATOR ONE)
// never appears in real content, so it's a safe delimiter.
export const FIELD_TOKEN_PREFIX = "KMF\u241F";
const FIELD_SEP = "\u241F";

/** Parse a field-chip token → {tid, label, value}, or null if ``text`` isn't a token.
 *  ``value`` is "" for an open (unfilled) field. */
export function parseFieldToken(text: string): { tid: string; label: string; value: string } | null {
  if (!text.startsWith(FIELD_TOKEN_PREFIX)) return null;
  const parts = text.slice(FIELD_TOKEN_PREFIX.length).split(FIELD_SEP);
  if (parts.length < 2) return null;
  return { tid: parts[0], label: parts[1], value: parts[2] ?? "" };
}

/** Render a section's raw markdown for the READ view: every ⟦TODO⟧ field — open OR filled —
 *  becomes a clickable chip token (a bordered control). Filled fields show their value and
 *  stay editable; open fields show the label. Mirrors the backend overlay so the document
 *  reads naturally. */
export function renderSectionRead(
  content: string,
  sectionKey: string,
  todos: KnowMeTodo[],
  _highlightId?: string,
  label?: string,
): string {
  const valueById = new Map(todos.filter((t) => t.status === "done" && t.value.trim()).map((t) => [t.id, t.value.trim()]));
  return cleanSectionContent(content || "", label || "").replace(TODO_RE, (_m, rawLabel, rawKey) => {
    const fieldLabel = (rawLabel || "").trim();
    const key = (rawKey || "").trim();
    const slug = fieldLabel.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
    const tid = `${sectionKey}:${key || slug}`;
    const val = valueById.get(tid) || "";
    // Both open and filled fields render as a chip token (value as the 3rd segment when
    // filled). Backticks/separators in the label or value can't break the inline-code span.
    const safeLabel = fieldLabel.replace(/[`\u241F]/g, "'");
    const safeVal = val.replace(/[`\u241F]/g, "'");
    return `\`${FIELD_TOKEN_PREFIX}${tid}${FIELD_SEP}${safeLabel}${FIELD_SEP}${safeVal}\``;
  });
}
