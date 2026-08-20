// Presentation constants + helpers for the AMBA Reference Set editor and coverage matrix.
//
// The metric catalog itself is NOT hard-coded here any more. It is served by
// `GET /amba/catalog`, which is generated from the vendored upstream Azure Monitor Baseline
// Alerts snapshot, so the editor's metric/threshold/window suggestions always match the
// AMBA release the baseline was imported from. Free-text metrics remain allowed.

import type { AmbaAlertRef, AmbaAlertType, AmbaCatalog, AmbaPattern, AmbaTier } from "../api";

export const AMBA_OPERATORS = [
  "GreaterThan",
  "GreaterThanOrEqual",
  "LessThan",
  "LessThanOrEqual",
  "GreaterOrLessThan",
  "Equals",
] as const;

export const AMBA_OPERATOR_SYMBOL: Record<string, string> = {
  GreaterThan: ">",
  GreaterThanOrEqual: "≥",
  LessThan: "<",
  LessThanOrEqual: "≤",
  GreaterOrLessThan: "≷",
  Equals: "=",
};

export const AMBA_SEVERITIES = ["critical", "error", "warning", "info"] as const;
export const AMBA_CATEGORIES = ["availability", "performance", "security"] as const;
export const AMBA_ALERT_TYPES: AmbaAlertType[] = ["metric", "log", "activitylog"];
export const AMBA_TIERS: AmbaTier[] = ["core", "recommended", "optional"];
export const AMBA_PATTERNS: AmbaPattern[] = ["alz", "hpc", "avd", "rag", "avs"];
export const AMBA_AGGREGATIONS = ["", "Average", "Minimum", "Maximum", "Total", "Count"];
export const AMBA_SENSITIVITIES = ["Low", "Medium", "High"] as const;
export const AMBA_WINDOWS = ["PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D"];
export const AMBA_FREQUENCIES = ["PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D"];
export const AMBA_UNITS = ["%", "count", "ms", "s", "bytes", "bytes/s", "RU", "bps", "flag", "ncores"];

export const CATEGORY_COLOR: Record<string, string> = {
  availability: "#dc2626",
  performance: "#2563eb",
  security: "#b91c1c",
};

export const ALERT_TYPE_LABEL: Record<string, string> = {
  metric: "Metric",
  log: "Log search",
  activitylog: "Activity log",
};

export const ALERT_TYPE_CLS: Record<string, string> = {
  metric: "bg-blue-50 text-blue-700",
  log: "bg-purple-50 text-purple-700",
  activitylog: "bg-teal-50 text-teal-700",
};

export const TIER_LABEL: Record<string, string> = {
  core: "Core",
  recommended: "Recommended",
  optional: "Optional",
};

/** Core = shipped in an official AMBA policy/initiative; optional = upstream-hidden. */
export const TIER_CLS: Record<string, string> = {
  core: "bg-emerald-100 text-emerald-800",
  recommended: "bg-slate-100 text-slate-700",
  optional: "bg-gray-100 text-gray-500",
};

export const TIER_HINT: Record<string, string> = {
  core: "Shipped in an official AMBA policy initiative — the opinionated baseline.",
  recommended: "Published on the AMBA site; deploy at your discretion.",
  optional: "Hidden upstream (experimental or noisy). Off by default.",
};

export const PATTERN_LABEL: Record<string, string> = {
  alz: "Azure Landing Zones",
  hpc: "High Performance Compute",
  avd: "Azure Virtual Desktop",
  rag: "AI / RAG workload",
  avs: "Azure VMware Solution",
};

/** ISO-8601 duration → a compact human label ("PT15M" → "15m"). */
export function durationLabel(value: string): string {
  return (value || "").replace("PT", "").replace("P", "").toLowerCase() || "—";
}

/** A readable threshold: no trailing zeros, byte counts scaled, unit spaced off the number. */
export function thresholdLabel(value: number | null | undefined, unit: string): string {
  if (value == null || Number.isNaN(value)) return "(exists)";
  const u = (unit || "").trim();
  if ((u === "bytes" || u === "bytes/s") && Math.abs(value) >= 1024) {
    const suffixes = ["B", "KB", "MB", "GB", "TB", "PB"];
    let scaled = Math.abs(value);
    let i = 0;
    while (scaled >= 1024 && i < suffixes.length - 1) {
      scaled /= 1024;
      i += 1;
    }
    const rendered = scaled >= 10 || Number.isInteger(scaled) ? scaled.toFixed(0) : scaled.toFixed(1);
    return `${value < 0 ? "-" : ""}${rendered} ${suffixes[i]}${u === "bytes/s" ? "/s" : ""}`;
  }
  const rendered = Number.isInteger(value) ? String(value) : String(value);
  return !u || u === "%" ? `${rendered}${u}` : `${rendered} ${u}`;
}

export type CatalogMetric = Partial<AmbaAlertRef> & { key: string; name: string };

/** Baseline entries the upstream catalog publishes for an ARM type. */
export function catalogFor(catalog: AmbaCatalog | undefined, armType: string): CatalogMetric[] {
  const spec = catalog?.types?.[(armType || "").toLowerCase()];
  return (spec?.alerts ?? []) as CatalogMetric[];
}

/** Every ARM type the upstream catalog knows about (for the "add resource type" picker). */
export function knownArmTypes(
  catalog: AmbaCatalog | undefined,
): { type: string; label: string; category: string; source: string; alertCount: number }[] {
  if (!catalog?.types) return [];
  return Object.entries(catalog.types)
    .map(([type, spec]) => ({
      type,
      label: spec.display,
      category: spec.category,
      source: spec.source,
      alertCount: spec.alerts.length,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

/** A blank alert definition with every field of the extended schema populated. */
export function blankAlert(overrides: Partial<AmbaAlertRef> = {}): AmbaAlertRef {
  return {
    key: "",
    guid: "",
    name: "",
    description: "",
    why: "",
    alert_type: "metric",
    amba_category: "performance",
    severity: "warning",
    severity_num: 2,
    tier: "recommended",
    patterns: [],
    metric: "",
    metric_namespace: "",
    counter_name: "",
    operator: "GreaterThan",
    threshold: null,
    unit: "%",
    criterion_type: "StaticThresholdCriterion",
    alert_sensitivity: null,
    failing_periods: null,
    auto_mitigate: null,
    time_aggregation: "",
    window_size: "PT5M",
    evaluation_frequency: "PT5M",
    dimensions: [],
    dimension_filter: "",
    activity_log: {},
    log_query: "",
    visible: true,
    verified: false,
    default_enabled: true,
    requires_action_group: true,
    deployable: true,
    references: [],
    deployments: [],
    policy_alert_name: "",
    policy_scope: "",
    threshold_override_tag: "",
    amba_tags: [],
    source: "local",
    ...overrides,
  };
}

/** Turn a catalog entry into a full, editable alert definition. */
export function fromCatalog(entry: CatalogMetric): AmbaAlertRef {
  return blankAlert({ ...entry, source: (entry.source as "amba" | "local") ?? "amba" });
}

const SEVERITY_BY_NUM: Record<number, string> = {
  0: "critical",
  1: "error",
  2: "warning",
  3: "info",
  4: "info",
};
const NUM_BY_SEVERITY: Record<string, number> = { critical: 0, error: 1, warning: 2, info: 3 };

export function severityLabel(num: number | null | undefined): string {
  return typeof num === "number" ? SEVERITY_BY_NUM[num] ?? "info" : "info";
}

export function severityNumber(label: string): number {
  return NUM_BY_SEVERITY[label] ?? 2;
}

/** One-line, plain-English read-back of an alert definition. */
export function sentence(a: AmbaAlertRef): string {
  const ag = a.requires_action_group ? " · needs action group" : "";
  const win = durationLabel(a.window_size);
  const freq = durationLabel(a.evaluation_frequency);
  const cadence = freq && freq !== win ? `${win} window, checked every ${freq}` : `over ${win}`;

  if (a.alert_type === "activitylog") {
    const activity = a.activity_log ?? {};
    const bits = [activity["category"], activity["incidentType"], activity["operationName"]]
      .filter(Boolean)
      .join(" · ");
    return `Activity log${bits ? ` — ${bits}` : ""}${ag}`;
  }
  if (a.alert_type === "log") {
    const table = (a.log_query || "").trim().split(/[\s|]/)[0] || "log query";
    const op = AMBA_OPERATOR_SYMBOL[a.operator] || a.operator;
    const thr = a.threshold != null ? thresholdLabel(a.threshold, a.unit) : "(any result)";
    return `Log search on ${table} ${op} ${thr}, ${cadence}${ag}`;
  }
  if (a.criterion_type === "DynamicThresholdCriterion") {
    const periods = a.failing_periods;
    const fail = periods?.min_failing_periods_to_alert && periods?.number_of_evaluation_periods
      ? `, ${periods.min_failing_periods_to_alert} of ${periods.number_of_evaluation_periods} periods failing`
      : "";
    return `${a.metric || "metric"} vs dynamic threshold (${a.alert_sensitivity || "Medium"} sensitivity), ${cadence}${fail}${ag}`;
  }
  const op = AMBA_OPERATOR_SYMBOL[a.operator] || a.operator;
  const thr = thresholdLabel(a.threshold, a.unit);
  const dims = (a.dimensions ?? [])
    .map((d) => `${d.name} ${d.operator === "Exclude" ? "∉" : "∈"} {${d.values.join(", ")}}`)
    .join(", ");
  return `${a.metric || "metric"} ${op} ${thr}, ${cadence}${dims ? ` where ${dims}` : ""}${ag}`;
}
