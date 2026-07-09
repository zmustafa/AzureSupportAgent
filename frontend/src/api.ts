const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000/api";
export { API_BASE };

export interface Chat {
  id: string;
  title: string;
  provider: string | null;
  model: string | null;
  connection_id: string | null;
  thinking_level?: string;
  agent_id?: string | null;
  workload_id?: string | null;
  archived: boolean;
  pinned: boolean;
  created_at: string;
}

export interface ActivityStep {
  kind: "reasoning" | "tool";
  text?: string;
  name?: string;
  args?: unknown;
  status?: string;
  summary?: string;
  duration?: number;
}

export interface Message {
  id: string;
  role: string;
  content: string;
  activity?: ActivityStep[] | null;
  created_at: string;
  // Provider/model that produced an assistant message (for attribution).
  provider?: string | null;
  model?: string | null;
  // Wall-clock processing time for an assistant turn, in milliseconds.
  duration_ms?: number | null;
  // Structured deep-investigation result (phases + hypothesis tree + conclusion).
  investigation?: Investigation | null;
  // Optimistic-only: images the user attached (not persisted server-side).
  images?: string[];
}

// A ticketing connector the chat can be sent to (ServiceNow / Jira / XSOAR).
export interface TicketConnector {
  id: string;
  name: string;
  type: string;
  label: string;
}
// Result of creating a ticket from a chat.
export interface TicketResult {
  ok: boolean;
  number?: string;
  url?: string;
  detail?: string;
  connector_type?: string;
  attached?: boolean;        // whether the chat PDF was attached to the ticket
  attach_error?: string;     // non-fatal: ticket created but PDF attach failed
}

export interface Me {
  subject: string;
  email: string;
  tenant_id: string;
  role: string;
  permissions?: string[];
  display_name?: string;
  auth_source?: string;
  must_change_password?: boolean;
  assigned_roles?: string[];
  active_role?: string;
  default_role?: string;
  first_name?: string;
  last_name?: string;
  language?: string;
}

export interface AuthConfig {
  local_login_enabled: boolean;
  providers: { id: string; type: string; label: string }[];
}

export interface ActiveLlm {
  provider: string;
  model: string;
}

export class HttpError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    // API responses are dynamic and must never come from the browser HTTP cache
    // (e.g. a stale /me after switching roles). React Query handles app-level caching.
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

async function httpBlob(path: string, init?: RequestInit): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(res.status, detail);
  }
  return res.blob();
}

/** Absolute API origin, e.g. for SSO redirects (full-page navigations). */
export const apiBase = API_BASE;

/** POST a multipart form (file upload). Lets the browser set the multipart boundary. */
async function httpUpload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: form,
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON */
    }
    throw new HttpError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

/** Trigger a browser download of a Blob with a filename. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- Identity dashboard ---------------------------------------------------------
export type IdentityFinding = {
  id: string;
  kind: string;
  title: string;
  detail: string;
  severity: "critical" | "error" | "warning" | "info" | "ok";
  subject: string;
  subject_id: string;
  expires_at?: string | null;
  days_left?: number | null;
  workload_id?: string | null;
  workload_name?: string | null;
  remediation: string;
};

export type IdentityGroupKey =
  | "expiring_credentials"
  | "ownerless_apps"
  | "ca_gaps"
  | "users_without_mfa"
  | "keyvault_expiry";

export type IdentityOverview = {
  generated_at: string;
  days: number;
  tenant_id: string;
  connection_configured: boolean;
  kpis: {
    expiring_secrets: number;
    expiring_certs: number;
    ownerless_apps: number;
    users_without_mfa: number;
    ca_gaps: number;
    keyvault_expiring: number;
  };
  group_severity: Record<IdentityGroupKey, string>;
  groups: Record<IdentityGroupKey, IdentityFinding[]>;
  errors: Partial<Record<IdentityGroupKey, string>>;
  meta: { mfa_sampled?: boolean; mfa_scanned?: number };
  ttl_s: number;
  age_seconds: number | null;
  stale: boolean;
  never_loaded?: boolean;
};

// ---- PIM / JIT lifecycle review (Identity tab) ----------------------------------
export type PimGroupKey = "standing_access" | "stale_eligible" | "stale_active" | "activation_review";

export type PimFinding = {
  id: string;
  kind: string;
  title: string;
  detail: string;
  severity: "critical" | "error" | "warning" | "info" | "ok";
  subject: string;
  subject_id: string;
  role: string;
  role_tier: "tier0" | "tier1" | "tier2";
  scope: string;
  assignment_type: "eligible" | "active" | "activated" | "";
  last_activated_at?: string | null;
  days_idle?: number | null;
  activation_count_90d?: number | null;
  expires_at?: string | null;
  days_left?: number | null;
  workload_id?: string | null;
  workload_name?: string | null;
  remediation: string;
};

export type PimOverview = {
  generated_at: string;
  tenant_id: string;
  connection_configured: boolean;
  kpis: {
    standing_access: number;
    high_priv_standing: number;
    stale_eligible: number;
    stale_active: number;
    activations: number;
  };
  group_severity: Record<PimGroupKey, string>;
  groups: Record<PimGroupKey, PimFinding[]>;
  errors: Partial<Record<PimGroupKey, string>>;
  meta: { source?: string; thresholds?: Record<string, number> };
  ttl_s: number;
  age_seconds: number | null;
  stale: boolean;
  never_loaded?: boolean;
};


export type AppRegCredential = {
  type: "secret" | "certificate";
  displayName: string;
  endDateTime?: string | null;
  daysUntilExpiry: number | null;
};

export type AppRegPermission = {
  api: string;
  value: string;
  type: "Application" | "Delegated";
  risk: "high" | "medium" | "low";
};

export type AppRegistration = {
  id: string;
  appId: string;
  displayName: string;
  signInAudience: string;
  createdDateTime?: string | null;
  publisherDomain: string;
  tags: string[];
  secretsCount: number;
  certsCount: number;
  credentials: AppRegCredential[];
  nextExpiryDays: number | null;
  expiredCredentials: number;
  applicationPermissionsCount: number;
  delegatedPermissionsCount: number;
  permissions: AppRegPermission[];
  owners: string[];
  ownerless: boolean;
  highRisk: boolean;
};

export type AppRegFacet = { value: string; count: number };

export type AppRegistrationsResponse = {
  generated_at: string;
  tenant_id: string;
  connection_configured: boolean;
  source: "demo_dummy_data" | "microsoft_graph" | "unavailable";
  note: string;
  // Set when a configured connection could NOT enumerate (Graph auth/config error or live
  // failure). The view shows the actionable error instead of substituting demo data.
  connection_failed?: boolean;
  apps: AppRegistration[];
  facets: {
    audiences: AppRegFacet[];
    permissions: AppRegFacet[];
    owners: AppRegFacet[];
  };
  summary: {
    total: number;
    withSecrets: number;
    withCerts: number;
    expiringSoon: number;
    expired: number;
    highRisk: number;
    ownerless: number;
    applicationPerms: number;
    delegatedPerms: number;
  };
  cached: boolean;
  never_loaded?: boolean;
  fetched_at: string;
  age_seconds: number | null;
  // IU3 — set when the live listing hit the per-refresh cap (UI shows a "first N" notice).
  truncated?: boolean;
  limit?: number;
};

// ---- AMBA Monitoring Coverage ---------------------------------------------------
// Shared across the three coverage dashboards: the "All Resources" tab lists every
// in-scope resource with a flag for whether the reference set covers its type.
export type CoverageResource = {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  in_reference: boolean;
};

export type AmbaStatus = "present" | "missing" | "misconfigured";

export type AmbaRecommended = {
  metric: string;
  operator: string;
  threshold: number | null;
  unit: string;
  window: string;
  requires_action_group: boolean;
};

export type AmbaObserved = {
  rule_id?: string;
  rule_name?: string;
  enabled?: boolean;
  has_action_group?: boolean;
  observed_thresholds?: number[];
  issues?: string[];
};

export type AmbaCell = {
  alert_key: string;
  alert_name: string;
  amba_category: string;
  severity: string;
  status: AmbaStatus;
  recommended: AmbaRecommended;
  observed: AmbaObserved;
  why: string;
};

export type AmbaRow = {
  resource_id: string;
  resource_name: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  tags: Record<string, string>;
  cells: AmbaCell[];
};

export type AmbaGroup = {
  resource_type: string;
  display: string;
  category: string;
  recommended_alerts: { key: string; name: string; amba_category: string; severity: string }[];
  rows: AmbaRow[];
  present: number;
  missing: number;
  misconfigured: number;
  coverage_pct: number;
};

export type AmbaGap = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  alert_key: string;
  alert_name: string;
  amba_category: string;
  severity: string;
  status: AmbaStatus;
  recommended: AmbaRecommended;
  observed: AmbaObserved;
  why: string;
};

export type AmbaCoverage = {
  generated_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  connection_configured: boolean;
  source: string;
  demo: boolean;
  coverage_pct: number;
  kpis: {
    total_resources_in_baseline: number;
    alerts_present: number;
    alerts_missing: number;
    alerts_misconfigured: number;
    recommended_total: number;
  };
  groups: AmbaGroup[];
  gaps: AmbaGap[];
  error: string;
  ttl_s: number;
  age_seconds: number | null;
  stale: boolean;
  all_resources: CoverageResource[];
  // False when no saved scan exists for the scope yet (the GET is cached-only — it never
  // triggers a live compute), so the UI shows an empty "run first scan" state.
  report_exists?: boolean;
};

export type AmbaAlertRef = {
  key: string;
  name: string;
  amba_category: "availability" | "performance" | "security";
  signal: "metric" | "log";
  metric: string;
  operator: string;
  threshold: number | null;
  unit: string;
  window: string;
  severity: string;
  requires_action_group: boolean;
  why: string;
};

export type AmbaReference = {
  version: number;
  updated_at: string;
  updated_by: string;
  builtin_seed_version: number;
  types: Record<string, { display: string; category: string; alerts: AmbaAlertRef[] }>;
};

export type AmbaReferenceRevision = {
  id: string;
  version: number;
  created_at: string;
  by: string;
  reason: string;
  type_count: number;
  alert_count: number;
};

export type AmbaChangeRequest = {
  id: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  gap_count: number;
  iac_format: string;
  iac_text?: string;
  status: "pending" | "approved" | "rejected" | "applied";
  requested_by: string;
  requested_at: string;
  decided_by: string;
  decided_at: string;
  reason: string;
};

// ---- Telemetry Coverage (diagnostic settings auditor) ---------------------------
export type TelemetryStatus = "none" | "partial" | "compliant";

export type TelemetryCategory = {
  key: string;
  name: string;
  kind: "log" | "metric";
  group: "audit" | "security" | "operational" | "performance";
  recommended: boolean;
  why: string;
};

export type TelemetryDestination = {
  workspace_id: string;
  storage_account_id: string;
  event_hub: string;
  retention_days: number;
};

export type TelemetryRow = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  tags: Record<string, string>;
  status: TelemetryStatus;
  settings_count: number;
  enabled_categories: string[];
  recommended_categories: string[];
  missing_categories: string[];
  missing_audit_categories: string[];
  destinations: TelemetryDestination[];
  drift_workspaces: string[];
  has_drift: boolean;
};

export type TelemetryGroup = {
  resource_type: string;
  display: string;
  note: string;
  recommended_categories: TelemetryCategory[];
  rows: TelemetryRow[];
  none: number;
  partial: number;
  compliant: number;
  coverage_pct: number;
};

export type TelemetryGap = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  status: TelemetryStatus;
  missing_categories: string[];
  missing_audit_categories: string[];
  has_drift: boolean;
  drift_workspaces: string[];
  severity: string;
};

export type TelemetryCoverage = {
  generated_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  connection_configured: boolean;
  source: string;
  demo: boolean;
  coverage_pct: number;
  kpis: {
    total_resources_in_reference: number;
    with_any_diag: number;
    pct_with_any_diag: number;
    with_all_categories: number;
    pct_with_all_categories: number;
    to_approved_workspace: number;
    pct_to_approved: number;
    unknown_destinations: number;
    unreadable: number;
  };
  groups: TelemetryGroup[];
  gaps: TelemetryGap[];
  error: string;
  ttl_s: number;
  age_seconds: number | null;
  stale: boolean;
  all_resources: CoverageResource[];
  report_exists?: boolean;
};

export type TelemetryReference = {
  version: number;
  updated_at: string;
  updated_by: string;
  builtin_seed_version: number;
  types: Record<string, { display: string; note: string; categories: TelemetryCategory[] }>;
};

export type TelemetryReferenceRevision = {
  id: string;
  version: number;
  created_at: string;
  by: string;
  reason: string;
  type_count: number;
  category_count: number;
};

export type TelemetryWorkspace = {
  id: string;
  name: string;
  resourceGroup: string;
  subscriptionId: string;
  location: string;
};

// ---- Backup & DR Coverage -------------------------------------------------------
export type BackupDrCellStatus = "green" | "amber" | "red" | "na";

export type BackupDrCell = {
  check: string;
  status: BackupDrCellStatus;
  value: string;
  detail: string;
};

export type BackupDrRow = {
  resource_id: string;
  resource_name: string;
  resource_group: string;
  subscription_id: string;
  region: string;
  backup_region: string;
  status: "green" | "amber" | "red";
  cells: BackupDrCell[];
  state: Record<string, unknown>;
};

export type BackupDrGroup = {
  resource_type: string;
  display: string;
  category: string;
  note: string;
  checks: string[];
  rows: BackupDrRow[];
  red: number;
  amber: number;
  green: number;
  coverage_pct: number;
};

export type BackupDrPair = {
  name: string;
  primary_region: string;
  secondary_region: string;
  replication_health: string;
  healthy: boolean;
  last_failover_test_age_days: number | null;
  stale: boolean;
  protected_items: number;
};

export type BackupDrGap = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  resource_group: string;
  subscription_id: string;
  region: string;
  backup_region: string;
  status: string;
  failed_checks: string[];
  vault_name: string;
  policy: string;
  dr_target_region: string;
  severity: string;
};

export type BackupDrCoverage = {
  generated_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  connection_configured: boolean;
  source: string;
  demo: boolean;
  scorecard: {
    total: number;
    protected: number;
    pct_protected: number;
    pct_offsite: number;
    pct_recent_job: number;
    dr_pairs: number;
    dr_pairs_stale: number;
    dr_pairs_unhealthy: number;
    last_drill_days: number | null;
  };
  groups: BackupDrGroup[];
  dr_pairs: BackupDrPair[];
  gaps: BackupDrGap[];
  error: string;
  ttl_s: number;
  age_seconds: number | null;
  stale_cache: boolean;
  all_resources: CoverageResource[];
  report_exists?: boolean;
};

export type BackupDrReference = {
  version: number;
  updated_at: string;
  updated_by: string;
  builtin_seed_version: number;
  types: Record<string, { display: string; category: string; note: string; checks: string[] }>;
};

export type BackupDrReferenceRevision = {
  id: string;
  version: number;
  created_at: string;
  by: string;
  reason: string;
  type_count: number;
  check_count: number;
};

// --- Retirement & Breaking-Change Radar ---------------------------------------
export type RadarImpactedResource = {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  region: string;
  subscription_id: string;
  workload_id: string;
  workload_name: string;
  owner: string;
  owner_source: string;
  unowned: boolean;
};

export type RadarEvent = {
  id: string;
  tracking_id: string;
  sources: string[];
  title: string;
  summary: string;
  service: string;
  feature: string;
  change_type: "retirement" | "breaking_change";
  retirement_date: string;
  recommended_replacement: string;
  migration_url: string;
  rule_id: string;
  impacted_resources: RadarImpactedResource[];
  impacted_count: number;
  owner: string;
  unowned: boolean;
  days_until: number | null;
  severity: "red" | "amber" | "grey";
  status: string;
  assignee: string;
  waive_reason: string;
};

export type RadarModelItem = {
  id: string;
  account: string;
  deployment: string;
  model: string;
  model_version: string;
  region: string;
  resource_group: string;
  subscription_id: string;
  stage: string;
  ga_date: string;
  deprecation_date: string;
  retirement_date: string;
  replacement: string;
  days_until: number | null;
  severity: "red" | "amber" | "grey";
  matched: boolean;
};

export type RadarRailItem = {
  id: string;
  title: string;
  service: string;
  change_type: string;
  days_until: number | null;
  impacted_count: number;
  severity: "red" | "amber" | "grey";
};

export type RadarSnapshot = {
  generated_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  connection_configured: boolean;
  source: string;
  demo: boolean;
  error: string;
  rail: RadarRailItem[];
  events: RadarEvent[];
  model_items: RadarModelItem[];
  counts: {
    total: number;
    retirement: number;
    breaking_change: number;
    red: number;
    amber: number;
    grey: number;
    unowned: number;
    impacted_total: number;
    models: number;
    models_at_risk: number;
  };
  ttl_s: number;
  age_seconds: number | null;
  stale_cache: boolean;
  never_loaded?: boolean;
};

// ---- Workload Mission Control --------------------------------------------------
export type MissionSystem = {
  key: string;
  label: string;
  icon: string;
  status: string; // queued|running|done|skipped|fail|error|idle
  headline: string;
  detail?: string;
  score: number | null;
  attention: boolean;
  link: string;
  result_ref?: { kind: string; id?: string; workload_id?: string } | null;
  error?: string;
  started_at?: string;
  ended_at?: string;
  informational?: boolean;
  age_seconds?: number | null;
  fresh?: boolean;
};

export type MissionLog = { ts: string; key: string; message: string };

export type Mission = {
  id: string;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  status: string; // queued|running|succeeded|partial|failed|cancelled
  readiness: string; // go|warn|nogo|unknown
  force: boolean;
  trigger: string;
  systems_total: number;
  systems_done: number;
  systems_attention: number;
  systems: MissionSystem[];
  log?: MissionLog[];
  error?: string;
  created_at: string;
  started_at: string;
  ended_at: string;
  duration_ms?: number | null;
};

export type MissionState = {
  workload_id: string;
  workload_name?: string;
  connection_id?: string;
  systems: MissionSystem[];
  error?: string;
};

export type MissionSystemDef = { key: string; label: string; icon: string; informational: boolean };

export type RadarClassificationRule = {
  id: string;
  keywords: string[];
  change_type: string;
  service: string;
  replacement: string;
  migration_url: string;
  planned_date: string;
};

export type RadarModelLifecycle = {
  model: string;
  version: string;
  stage: string;
  ga_date: string;
  deprecation_date: string;
  retirement_date: string;
  replacement: string;
};

export type RadarReference = {
  version: number;
  updated_at: string;
  updated_by: string;
  builtin_seed_version: number;
  classification_rules: RadarClassificationRule[];
  model_lifecycle: RadarModelLifecycle[];
};

export type RadarReferenceRevision = {
  id: string;
  version: number;
  created_at: string;
  by: string;
  reason: string;
  rule_count: number;
  model_count: number;
};

// --- Telemetry Intelligence (AI correlation & triage over App Insights) -------
export type TeleIntelComponent = {
  id: string;
  name: string;
  app_id: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  workspace_id: string;
  mode: string;
};

export type TeleIntelOverview = {
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  components: TeleIntelComponent[];
  predicate?: string;
  sli_context: string;
  connection_configured: boolean;
  source: string;
  demo: boolean;
  error: string;
};

export type TeleIntelEvidence = {
  label: string;
  kql: string;
  rows: Record<string, unknown>[];
  ok: boolean;
};

export type TeleIntelTriage = {
  component?: { id: string; name: string };
  generated_at?: string;
  summary: {
    operation: string;
    failure_rate_pct: number;
    failed: number;
    total: number;
    top_dependency: string;
    top_dependency_type: string;
    dependency_correlation_pct: number;
    top_exception: string;
    probable_trigger: string;
    trigger_target: string;
    change_count: number;
  };
  hypothesis: string;
  evidence: TeleIntelEvidence[];
  has_spike: boolean;
  error?: string;
};

export type TeleIntelTimelinePoint = Record<string, number | string>;

export type TeleIntelTimeline = {
  series_keys: string[];
  points: TeleIntelTimelinePoint[];
  change_events: { timestamp: string; change_type: string; target: string; target_id: string }[];
  bin_minutes: number;
  signal_count: number;
  notes: string;
  generated_at: string;
};

export type TeleIntelSmartDetectionItem = {
  display_name: string;
  rule_name: string;
  severity: string;
  components: string[];
  occurrences: number;
};

export type TeleIntelSmartDetection = {
  items: TeleIntelSmartDetectionItem[];
  component_count: number;
  detection_count: number;
  note: string;
};

export type TeleIntelSpan = {
  timestamp: string;
  kind: string;
  name: string;
  target: string;
  result_code: string;
  duration_ms: number | null;
  failed: boolean;
  id: string;
  parent_id: string;
};

export type TeleIntelTransaction = {
  ok: boolean;
  operation_id?: string;
  kql?: string;
  spans: TeleIntelSpan[];
  total_ms?: number;
  failing_step?: string;
  narration?: string;
  error?: string;
};

export type TeleIntelCodeOpt = {
  items: { type: string; issue: string; impact: string; function: string }[];
  note: string;
};

// --- Performance Profiler (profile a workload against AMBA) --------------------
export type PerfMetricCell = {
  alert_key: string;
  metric: string;
  name: string;
  amba_category: string;
  severity: string;
  unit: string;
  operator: string;
  threshold: number | null;
  aggregation: string;
  higher_is_worse: boolean;
  current: number | null;
  peak: number | null;
  avg: number | null;
  observed: number | null;
  pct_of_threshold: number | null;
  headroom_pct: number | null;
  trend_pct: number;
  state: "healthy" | "approaching" | "breaching" | "no_data";
  why: string;
  series: { timestamp: string; value: number }[];
};

export type PerfResourceRow = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  display: string;
  resource_group: string;
  subscription_id: string;
  region: string;
  score: number;
  state: "healthy" | "approaching" | "breaching" | "no_data";
  cells: PerfMetricCell[];
};

export type PerfBottleneck = {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  metric: string;
  metric_name: string;
  severity: string;
  state: string;
  observed: number | null;
  threshold: number | null;
  unit: string;
  pct_of_threshold: number | null;
  trend_pct: number;
  why: string;
};

export type PerfProfile = {
  generated_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  connection_configured: boolean;
  source: string;
  window: string;
  demo: boolean;
  error: string;
  scorecard: {
    workload_score: number;
    resources_profiled: number;
    breaching: number;
    approaching: number;
    healthy: number;
    bottleneck_count: number;
  };
  top_bottleneck: PerfBottleneck | null;
  bottlenecks: PerfBottleneck[];
  resources: PerfResourceRow[];
  // Every in-scope resource (not just AMBA-profiled types), for the "All Resources" tab.
  all_resources?: CoverageResource[];
  narrative?: string;
  ttl_s?: number;
  age_seconds?: number | null;
  stale_cache?: boolean;
  // run-history fields
  id?: string;
  run_at?: string;
  requested_window?: string;
  requested_start?: string;
  requested_end?: string;
  interval?: string;
  no_runs?: boolean;
};

// ---- Cleanup tab (cross-scope run cleanup, shared across 6 screens) --------------
export interface CleanupRun {
  id: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  run_at: string;
  size_bytes: number;
  deleted_at: string;
  demo?: boolean;
  // feature-specific meta (any of these may be present)
  workload_score?: number | null;
  score?: number | null;
  breaching?: number;
  resources_profiled?: number;
  total_changes?: number;
  critical_count?: number;
  headline?: number | null;
  resource_count?: number;
  status?: string;
  window?: string;
}
export interface CleanupStats {
  total_runs: number;
  active_runs: number;
  trashed_runs: number;
  total_bytes: number;
  trashed_bytes: number;
  scopes: number;
  oldest_run_at: string;
}
export interface CleanupData {
  runs: CleanupRun[];
  stats: CleanupStats;
}
export type CleanupResult = { count: number; freed_bytes?: number };

export type PerfRunSummary = {
  id: string;
  run_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  window: string;
  requested_window?: string;
  requested_start?: string;
  requested_end?: string;
  workload_score: number | null;
  resources_profiled: number;
  breaching: number;
  approaching: number;
  healthy: number;
  top_bottleneck: { resource_name: string; metric_name: string; pct_of_threshold: number | null; state: string } | null;
  demo: boolean;
  triggered_by: string;
  deleted_at?: string;
};

// ---- Performance Fleet (latest run per workload + mass launch) -------------------
export type PerfFleetRow = {
  workload_id: string;
  name: string;
  connection_id: string;
  criticality: string;
  environment: string;
  has_runs: boolean;
  run_id: string;
  run_at: string;
  window: string;
  workload_score: number | null;
  resources_profiled: number;
  breaching: number;
  approaching: number;
  healthy: number;
  top_bottleneck: { resource_name: string; metric_name: string; pct_of_threshold: number | null; state: string } | null;
  demo: boolean;
  age_seconds: number | null;
  stale: boolean;
};
export type PerfFleet = {
  workloads: PerfFleetRow[];
  ttl_s: number;
  default_window: string;
  total: number;
  profiled: number;
};

// ---- Coverage / posture trend (shared by the 4 dashboards) ----------------------
export type CoverageTrendPoint = { at: string; pct: number | null; extra?: Record<string, number>; demo?: boolean };
export type CoverageTrend = {
  feature: string;
  points: CoverageTrendPoint[];
  current: number | null;
  previous: number | null;
  delta: number | null;
  count: number;
  unit: string;
};

// ---- Coverage scan history (shared by Monitoring / Telemetry / Backup-DR) --------
export type CoverageFeature = "amba" | "telemetry" | "backupdr";
export type CoverageRunSummary = {
  id: string;
  run_at: string;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  headline: number | null;
  counts: Record<string, number>;
  resource_count: number;
  demo: boolean;
  triggered_by: string;
  deleted_at?: string;
};

// ---- Private Network Reachability Analyzer (netcheck) ---------------------------
export type NetCheckStep = {
  step: string;
  status: "ok" | "fail" | "warn" | "skip";
  evidence: string;
  command: string;
  raw: string;
  duration_ms: number;
};

export type NetCheckEvidence = {
  available: boolean;
  notes: string;
  effective_routes: Record<string, unknown>[];
  nsg_rules: { nsg: string; name: string; access: string; direction: string; protocol: string; destinationPortRange: string; priority: number | null }[];
  peerings: Record<string, unknown>[];
  matched_deny?: { nsg: string; name: string; access: string; direction: string; destinationPortRange: string; priority: number | null };
  error: string;
};

export type NetCheckRun = {
  id: string;
  key: string;
  architecture_id: string;
  source: string;
  source_vm_id: string;
  target: string;
  port: number;
  protocol: string;
  payload: Record<string, unknown>;
  steps: NetCheckStep[];
  verdict: "reachable" | "degraded" | "blocked";
  evidence: NetCheckEvidence;
  mismatch: { kind: string; detail: string } | null;
  path: { node_id: string; role: string; status?: string }[];
  created_at: string;
  created_by: string;
  demo?: boolean;
};

export type NetCheckSource = { id: string; display_name: string; vnet_label: string; disabled: boolean; linked?: boolean };
export type NetCheckDiff = { step: string; from: string; to: string };

export type NetCheckRunRequest = {
  architecture_id: string;
  source_vm_id?: string;
  source_host?: string;
  source_node_id?: string;
  target_node_id?: string;
  target_host?: string;
  port: number;
  protocol: string;
  payload?: Record<string, unknown>;
};

// ---- Private Endpoint Resolution Debugger (dnsdebug) ----------------------------
export type DnsStep = {
  step: string;
  status: "ok" | "fail" | "warn" | "skip";
  evidence: string;
  command: string;
  raw: string;
  duration_ms: number;
  source?: string;
};

export type DnsSourceResult = {
  source: string;
  vm_id: string;
  resolved_ip: string;
  classification: "private" | "public" | "nxdomain";
  misconfig_kind: string;
  verdict: string;
  custom_dns: string[];
  steps: DnsStep[];
};

export type DnsZoneFacts = {
  available: boolean;
  notes: string;
  expected_zone: string;
  zone_exists: boolean | null;
  linked_to_source_vnet: boolean | null;
  a_record_ip: string;
  custom_dns_servers: string[];
  error: string;
};

export type DnsDebugRun = {
  id: string;
  architecture_id: string;
  fqdn: string;
  source_vnet_id: string;
  sources: DnsSourceResult[];
  zone_facts: DnsZoneFacts;
  verdict: string;
  misconfig_kind: string;
  overall_classification: string;
  created_at: string;
  created_by: string;
  demo?: boolean;
};

export type DnsDebugSource = { id: string; display_name: string; vnet_label: string; disabled: boolean; linked?: boolean };
export type DnsDebugDiff = { source: string; from: string; to: string };

export type DnsDebugRunRequest = {
  architecture_id: string;
  source_vm_ids?: string[];
  fqdn: string;
  source_vnet_id?: string;
};

// ---- Evidence Locker (investigation snapshots) ----------------------------------
export type EvidenceScope = { kind: string; id: string; resource_ids: string[] };

export type EvidenceSnapshot = {
  id: string;
  name: string;
  scope: EvidenceScope;
  included: string[];
  retention_class: "standard" | "audit";
  tags: string[];
  finding_links: string[];
  sha256: string;
  size: number;
  section_counts: Record<string, number>;
  created_by: string;
  created_at: string;
  attachments: { type: string; connector?: string; ticket_id?: string; ticket_url?: string; note?: string; by?: string; at?: string }[];
  shares: { token: string; created_by: string; created_at: string; expires_at: string }[];
  deleted_at?: string;
  deleted_by?: string;
  demo?: boolean;
};

export type EvidenceDiff = {
  inventory: { added: { id: string; name: string; type: string }[]; removed: { id: string; name: string; type: string }[]; changed: { id: string; name: string; type: string; fields: Record<string, { from: unknown; to: unknown }> }[]; counts: { added: number; removed: number; changed: number } };
  findings: { added: Record<string, unknown>[]; removed: Record<string, unknown>[]; changed: { check_id: string; title: string; from: { status: string; severity: string }; to: { status: string; severity: string } }[]; counts: { added: number; removed: number; changed: number } };
};

export type EvidenceCreateRequest = {
  name: string;
  scope: EvidenceScope;
  included: string[];
  retention_class: string;
  tags: string[];
};

// ---- Reservations Monitor -------------------------------------------------------
export type ReservationItem = {
  id: string;
  order_id: string;
  display_name: string;
  term: string;
  billing_plan: string;
  created_date: string;
  expiry_date: string;
  provisioning_state: string;
  renew: boolean | null;
  utilization_pct: number | null;
  sku: string;
  reserved_resource_type: string;
  applied_scope_type: string;
  quantity: number | null;
  reservation_count: number;
  days_until: number | null;
  severity: "red" | "amber" | "grey";
  bucket: "expiring_soon" | "recently_expired" | "active" | "expired" | "unknown";
  expired: boolean;
};

export type ReservationsSnapshot = {
  generated_at: string;
  window_days: number;
  scope_id: string;
  source: string;
  demo: boolean;
  connection_configured: boolean;
  error: string;
  never_loaded?: boolean;
  items: ReservationItem[];
  counts: {
    total: number;
    expiring_soon: number;
    recently_expired: number;
    active: number;
    expired: number;
    in_window: number;
    red: number;
    amber: number;
    grey: number;
    non_renew: number;
    low_utilization: number;
  };
  ttl_s: number;
  age_seconds: number | null;
  stale_cache: boolean;
};

export type ReservationsDigestPreview = {
  items: ReservationItem[];
  expiring_soon: ReservationItem[];
  recently_expired: ReservationItem[];
  window_days: number;
  count: number;
  summary: string;
  html: string;
  never_loaded: boolean;
  error: string;
};

// ---- RBAC / Access Review -------------------------------------------------------
export type RbacRow = Record<string, string | boolean>;

export type RbacScopeFreshness = {
  scope: string;
  scopeType: string;
  displayName: string;
  subscriptionId: string;
  status: string;
  row_count: number;
  generated_at: string;
  age_seconds: number | null;
  collectors_total: number;
  collectors_attention: number;
  stale?: boolean;
  demo: boolean;
};

export type RbacDirectoryFreshness = {
  status: string;
  generated_at: string;
  age_seconds: number | null;
  row_count: number;
  role_def_count: number;
  principal_count: number;
  group_count: number;
  loaded: boolean;
};

export type RbacCollector = {
  collector: string;
  status: string;
  rowsAdded: number;
  durationSeconds: number;
  message: string;
  scope: string;
  scopeLabel: string;
};

export type RbacOverview = {
  tenant_id: string;
  generated_at: string;
  kpis: {
    total_assignments: number;
    unique_principals: number;
    privileged: number;
    data_plane: number;
    group_derived: number;
    owners: number;
    entra_roles: number;
    eligible: number;
    scopes: number;
    subscriptions: number;
  };
  group_severity: Record<string, string>;
  scopes: RbacScopeFreshness[];
  directory: RbacDirectoryFreshness;
  collectors: RbacCollector[];
  demo: boolean;
  never_loaded: boolean;
  ttl_s: number;
  connection_configured: boolean;
};

export type RbacAccessPage = {
  total: number;
  offset: number;
  limit: number;
  rows: RbacRow[];
  columns: string[];
};

export type RbacPivotItem = { label: string; count: number };
export type RbacPivots = { pivots: Record<string, RbacPivotItem[]>; labels: Record<string, string> };

export type RbacScopeNode = {
  id: string;
  name: string;
  type: "root" | "managementGroup" | "subscription";
  count: number;
  subscriptionIds: string[];
  inferred?: boolean;
  children: RbacScopeNode[];
};

export type RbacScopeTree = {
  root: RbacScopeNode;
  demo: boolean;
  subscription_count: number;
  mg_count: number;
};

export type RbacJob = {
  id: string;
  key: string;
  scope: string;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  progress_count: number;
  last_message: string;
  error: string;
} | null;

export type RbacRun = {
  id: string;
  scope: string;
  trigger: string;
  status: string;
  total_rows: number;
  privileged_count: number;
  unique_principals: number;
  kpis: Record<string, number>;
  scopes: { scope: string; displayName: string; row_count: number; status: string }[];
  diff: { baseline_run_id: string; added_privileged: string[]; removed_privileged: string[]; added_count: number; removed_count: number } | null;
  demo: boolean;
  triggered_by: string;
  started_at: string;
  ended_at: string;
  duration_ms: number | null;
};

export type RbacProgress = { seq: number; ts: string; level: "info" | "ok" | "warning" | "error"; message: string };

// ---- Central knowledge graph (/graph) ----
export type GraphNodeKind =
  | "tenant_connection"
  | "management_group"
  | "subscription"
  | "resource_group"
  | "resource"
  | "workload"
  | "architecture"
  | "architecture_memory"
  | "assessment_finding"
  | "rbac_principal"
  | "cost_bucket"
  | "retirement_item"
  | "change_event"
  | "coverage_gap"
  | "identity_finding";

export type GraphNode = {
  id: string;
  kind: GraphNodeKind;
  label: string;
  data: Record<string, any>;
  badges: Record<string, any>;
  expandable: boolean;
  parent?: string;
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: string;
  label: string;
};

export type GraphStats = { node_count: number; edge_count: number; by_kind: Record<string, number> };

export type GraphResult = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: GraphStats;
  truncated?: boolean;
  error?: string;
  generated_at?: string;
};

export type GraphOverview = GraphResult & {
  connection: AzureConnection;
  inventory_loaded: boolean;
  counts: { subscriptions: number; workloads: number; architectures: number; resources: number };
};

export type GraphNodeDetail = {
  found: boolean;
  detail?: string;
  node?: GraphNode;
  dossier?: Record<string, any>;
};

export type GraphPathResult = { found: boolean; path: string[]; hops: number; edges: string[] };
export type GraphBlastResult = {
  source: string;
  direct: string[];
  indirect: string[];
  by_depth: Record<string, string[]>;
  impacted_workloads: { id: string; label: string }[];
  impacted_count: number;
};
export type GraphDrift = {
  has_architecture: boolean;
  ok: { arm_id: string; name: string; type: string }[];
  documented_missing: { arm_id: string; name: string; type: string }[];
  live_uncontrolled: { arm_id: string; name: string; type: string }[];
  counts: { ok: number; documented_missing: number; live_uncontrolled: number };
  drift_score: number | null;
  summary: string;
};
export type GraphAnalytics = {
  stats: GraphStats;
  concentration_risk: { id: string; label: string; kind: string; betweenness: number; degree: number }[];
  communities: { size: number; kinds: Record<string, number>; sample: string[] }[];
  community_count: number;
  orphans: {
    unowned_resources: { id: string; name: string; type: string }[];
    unowned_count: number;
    workloads_without_architecture: { id: string; name: string }[];
    architectures_without_workload: { id: string; name: string }[];
  };
  candidate_workloads: { size: number; reason: string; resource_ids: string[]; types: { type: string; count: number }[]; resource_group: string; subscription_id: string }[];
};
export type GraphCompare = {
  left: { id: string; node_count: number };
  right: { id: string; node_count: number };
  only_left: { id: string; label: string; kind: string }[];
  only_right: { id: string; label: string; kind: string }[];
  shared: { id: string; label: string; kind: string }[];
};
export type GraphNarrative = { narrative: string; used_ai: boolean; summary: Record<string, any> };
export type GraphAskResult = { matched: string[]; count: number; filter: Record<string, any>; explanation: string; used_ai: boolean; nodes: GraphNode[] };
export type GraphView = {
  id: string;
  name: string;
  tenant_id: string;
  connection_id: string;
  scope_kind: string;
  scope_id: string;
  lens: string;
  layout: string;
  hidden_kinds: string[];
  expanded: string[];
  camera: Record<string, any>;
  overlays: string[];
  created_at: string;
  updated_at: string;
};

// ---- Ownership ------------------------------------------------------------------
export type OwnerLink = {
  user_id?: string;
  idp_id?: string;
  external_id?: string;
  entra_object_id?: string;
  upn?: string;
};

export type Owner = {
  id: string;
  kind: "person" | "team" | "service";
  display_name: string;
  email: string;
  source: "manual" | "app_user" | "entra" | "oidc_group" | "rbac";
  link: OwnerLink;
  members: string[];
  group_ref: Record<string, any>;
  delegate?: { owner_id?: string; until?: string; reason?: string };
  notes: string;
  tags: string[];
  assignment_count?: number;
  created_at: string;
  updated_at: string;
  deleted_at?: string;
};

export type OwnershipAssignment = {
  id: string;
  owner_id: string;
  subject_kind: "mg" | "subscription" | "resource_group" | "resource" | "workload" | "architecture";
  subject_id: string;
  subject_name: string;
  subscription_id: string;
  resource_group: string;
  role: "technical" | "business" | "security" | "cost" | "operations" | "escalation";
  primary: boolean;
  source: "manual" | "tag" | "rbac" | "workload" | "ai";
  confidence: number;
  notes: string;
  attested_at?: string;
  owner?: { id: string; display_name: string; email: string; kind: string } | null;
  created_at: string;
  updated_at: string;
  deleted_at?: string;
};

export type DirectoryHit = {
  source: "app_user" | "entra" | "oidc_group" | "rbac" | "manual";
  kind: "person" | "team" | "service";
  display_name: string;
  email: string;
  link: OwnerLink;
  group_ref?: Record<string, any>;
};

export type DirectorySearch = {
  query: string;
  results: DirectoryHit[];
  counts: { app_users: number; entra: number };
  notes: Record<string, string>;
};

export type ResolvedOwnerView = {
  owner_id: string;
  display_name: string;
  email: string;
  kind: string;
  role: string;
  primary: boolean;
  source: string;
  confidence: number;
  assignment_id: string;
  attested_at: string;
  delegate?: { owner_id: string; until?: string; reason?: string } | null;
};

export type ResolveResult = {
  subject_kind: string;
  subject_id: string;
  owners: ResolvedOwnerView[];
  source: "direct" | "tag" | "workload" | "inherited" | "none";
  inherited_from: { kind: string; id: string; name: string } | null;
  unowned: boolean;
};

export type OwnershipSubject = {
  subject_kind: string;
  subject_id: string;
  subject_name: string;
  owners: ResolvedOwnerView[];
  source: string;
  unowned: boolean;
};

// Section-wide scope for the Ownership module (Tenant / Subscription / Workload), mirroring
// the scope selectors on the Proactive Support modules.
export type OwnershipScope = {
  kind: "tenant" | "subscription" | "workload";
  workloadId: string;
  subId: string;
  subName: string;
};

function ownScopeQs(scope?: OwnershipScope, connectionId = ""): string {
  const qs = new URLSearchParams();
  if (scope && scope.kind !== "tenant") {
    qs.set("scope_kind", scope.kind);
    if (scope.kind === "workload" && scope.workloadId) qs.set("workload_id", scope.workloadId);
    if (scope.kind === "subscription" && scope.subId) qs.set("subscription_id", scope.subId);
  }
  if (connectionId) qs.set("connection_id", connectionId);
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export type OwnershipCoverageFinding = {
  id: string;
  severity: "error" | "warning" | "info";
  title: string;
  detail: string;
  count: number;
  resources: string[];
};

export type OwnershipCoverage = {
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  generated_at: string;
  demo: boolean;
  coverage_pct: number | null;
  kpis: { total: number; owned: number; unowned: number; owners: number; orphan_owners: number; prod_unowned: number };
  by_source: Record<string, number>;
  by_owner: { owner_id: string; label: string; email: string; count: number; via: Record<string, number> }[];
  unowned: { id: string; name: string; type: string; resource_group: string; subscription_id: string; owner: string; owner_source: string }[];
  orphans: { id: string; name: string; tag_owner: string }[];
  findings: OwnershipCoverageFinding[];
  error?: string;
  never_loaded?: boolean;
};

export type OwnerEstate = {
  owner: { id: string; display_name: string; email: string; kind: string; source: string; link: OwnerLink };
  total: number;
  by_kind: Record<string, number>;
  by_role: Record<string, number>;
  assignments: OwnershipAssignment[];
  linked: boolean;
};

export type OwnershipEstateResponse = {
  scope: "me" | "owner";
  principal?: { email: string; display_name: string };
  estates: OwnerEstate[];
  total_subjects?: number;
  matched_owners?: number;
};

export type OwnershipSuggestion = {
  id: string;
  subject_kind: string;
  subject_id: string;
  subject_name: string;
  candidate: { kind: string; display_name: string; email: string; source: string; link: OwnerLink };
  role: string;
  confidence: number;
  evidence: string[];
  signal: string;
};

// ---- Owner import / tag apply / tag revisions -----------------------------------
export interface OwnerImportPreviewRow {
  display_name: string;
  email: string;
  department: string;
  kind: string;
  role: string;
  notes: string;
  workload: string;
  subscription: string;
  resource_ids: string[];
  valid: boolean;
  has_subject: boolean;
}

export interface OwnerImportPreview {
  needs_sheet?: boolean;        // true when a multi-sheet workbook needs a sheet chosen first
  sheet_names?: string[];       // available sheet/tab names (single "csv" for CSV)
  columns: string[];
  sheet: string;
  row_count: number;
  sample: Record<string, string>[];
  rows: Record<string, string>[];
  mapping: Record<string, string>;
  confidence: number;
  explanation: string;
  ai: boolean;
  target_fields: string[];
  preview: {
    rows: OwnerImportPreviewRow[];
    total: number;
    preview_count: number;
    valid: number;
    invalid: number;
    with_subject: number;
  };
}

export interface OwnerImportResult {
  created: number;
  updated: number;
  assignments: number;
  skipped: number;
  unresolved_subjects: string[];
}

export interface OwnerTagApplyReq {
  connection_id?: string;
  scope_kind: "workload" | "subscription";
  workload_id?: string;
  subscription_id?: string;
  tag_key: string;
  value_source: "display_name" | "email" | "custom";
  custom_value?: string;
  overwrite: boolean;
}

export interface OwnerTagPlanItem {
  id: string;
  name: string;
  resource_group: string;
  subscription_id: string;
  before: Record<string, string>;
  after: Record<string, string>;
  owner: string;
  conflict: boolean;
  current: string;
  skipped: boolean;
  status: "apply" | "ok" | "conflict" | "no_owner";
}

export interface OwnerTagPlan {
  tag_key: string;
  value_source: string;
  items: OwnerTagPlanItem[];
  count: number;
  applicable: number;
  conflicts: number;
  no_owner: number;
  already_ok?: number;
  total_resources?: number;
}

export interface OwnerTagApplyResult {
  ok: boolean;
  error?: string;
  applied: number;
  failed: number;
  total: number;
  revision: TagRevision | null;
  results: { id: string; ok: boolean; error: string }[];
}

export interface TagRevision {
  id: string;
  created_at: string;
  actor: string;
  source: string;
  description: string;
  connection_id: string;
  scope: string;
  resource_count: number;
  applied: number;
  failed: number;
  status: "applied" | "reverted";
  reverted_at: string;
  reverted_by: string;
  reverts_id: string;
}

export interface TagRevisionDiffRow {
  id: string;
  name: string;
  before: Record<string, string>;
  after: Record<string, string>;
  added: Record<string, string>;
  removed: Record<string, string>;
  changed: Record<string, { from: string; to: string }>;
}

export interface TagRevertResult {
  ok: boolean;
  error?: string;
  reverted: number;
  failed: number;
  total: number;
  results: { id: string; ok: boolean; error: string }[];
  new_revision?: TagRevision;
}

export type AttestationItem = OwnershipAssignment & {
  owner?: { id: string; display_name: string; email: string; kind: string } | null;
  attestation_status: "never" | "stale" | "fresh";
  days_since: number | null;
};

export type LeaverRisk = {
  owner: { id: string; display_name: string; email: string };
  reason: string;
  orphaned_subjects: number;
  assignments: OwnershipAssignment[];
};

// ---------------------------------------------------------------- Quota Monitor
export type QuotaResult = {
  subscription_id: string;
  subscription_name: string;
  region: string;
  provider_namespace: string;
  service_name: string;
  quota_category: string;
  quota_name: string;
  sku_family: string;
  current_usage: number | null;
  limit: number | null;
  remaining: number | null;
  percent_used: number | null;
  unit: string;
  adjustable_status: string;
  source_type: string;
  collection_status: string;
  risk_level: string;
  recommendation: string;
  last_checked_utc: string;
  raw_provider_response: unknown;
  error_message: string;
  tenant_id?: string;
  tenant_name?: string;
};

export type QuotaProviderReg = {
  namespace: string;
  state: string;
  registered: boolean;
  remediation: string;
};

export type QuotaRegion = {
  name: string;
  display_name: string;
  regional_display_name: string;
  geography: string;
  geography_group: string;
  physical_location: string;
  category: string; // Recommended | Other
  has_availability_zones: boolean;
  paired_region: string;
};

export type QuotaSnapshot = {
  source: string;
  demo: boolean;
  connection_configured: boolean;
  never_loaded: boolean;
  error: string;
  status?: string;
  generated_at: string;
  subscription_id: string;
  subscription_name: string;
  regions_scanned: string[];
  categories_scanned: string[];
  thresholds: { watch: number; warning: number; critical: number };
  counts: Record<string, number>;
  by_provider: Record<string, { ok: number; error: number }>;
  provider_registration: QuotaProviderReg[];
  provider_errors: { provider: string; service: string; region: string; status: string; message: string }[];
  throttling: { events: number; min_remaining_reads: number | null };
  results: QuotaResult[];
  ai_summary: string;
  used_ai: boolean;
  capacity_note: string;
  // decoration
  scope_id?: string;
  ttl_s?: number;
  age_seconds?: number | null;
  stale_cache?: boolean;
};

export type QuotaCollectorMeta = {
  name: string;
  provider_namespace: string;
  service_label: string;
  categories: string[];
  scope: string;
  required_permissions: string[];
  dynamic: boolean;
  adjustable_default: string;
  source_default: string;
};

export type QuotaMeta = {
  collectors: QuotaCollectorMeta[];
  categories: string[];
  thresholds: { watch: number; warning: number; critical: number };
  capacity_note: string;
};

export type QuotaRun = {
  id: string;
  subscription_id: string;
  subscription_name: string;
  status: string;
  regions: string[];
  categories: string[];
  total_results: number;
  critical_count: number;
  warning_count: number;
  watch_count: number;
  counts: Record<string, number>;
  provider_errors: { provider: string; region: string; status: string; message: string }[];
  diff: { new_at_risk: string[]; recovered: string[] } | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
};

export type QuotaScanParams = {
  subscription_id: string;
  connection_id?: string;
  demo?: boolean;
  regions?: string[];
  categories?: string[];
  include_unused?: boolean;
};

// ---- Connection capability & blind-spot matrix -----------------------------------
export type CapStatus = "full" | "degraded" | "blind" | "disabled";
export interface CapCell {
  status: CapStatus;
  reason: string;
  remediation?: string;
}
export interface CapabilityColumn {
  key: string;
  label: string;
  desc: string;
}
export interface CapabilityConnection {
  id: string;
  display_name: string;
  auth_method: string;
  tenant_id: string;
  default_subscription: string;
  is_default: boolean;
  disabled: boolean;
  read_only: boolean;
  status: string;
  status_detail: string;
  last_tested: string;
  log_analytics_workspace_id: string;
  has_graph_token: boolean;
  caps: Record<string, CapCell>;
  blind_spots: string[];
  score: number;
}
export interface CapabilityMatrix {
  generated_at: string;
  live: boolean;
  capabilities: CapabilityColumn[];
  connections: CapabilityConnection[];
  summary: { connections: number; with_blind_spots: number; fully_capable: number };
}

// ---- Case Law: save an investigation RCA into architecture Memory ----------------
export interface SaveRcaResult {
  saved: boolean;
  already_saved?: boolean;
  architecture_id?: string;
  architecture_name?: string;
  memory_id?: string;
  needs_selection?: boolean;
  reason?: "no_linked_architecture" | "multiple_linked_architectures";
  candidates?: { id: string; name: string; workload_name?: string }[];
}

// ---- Durable Case Files ----------------------------------------------------------
export type CaseStatus =
  | "open" | "investigating" | "remediating" | "verifying" | "resolved" | "closed";
export interface CaseFile {
  id: string;
  title: string;
  summary: string;
  status: CaseStatus;
  severity: string;
  risk_score: number | null;
  confidence: number | null;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  architecture_id: string;
  finding_uids: string[];
  change_event_ids: string[];
  investigation_chat_id: string;
  investigation_message_id: string;
  evidence_snapshot_ids: string[];
  remediation_task_id: string;
  verification_json: Record<string, unknown> | null;
  assignee: string;
  opened_by: string;
  opened_at: string;
  updated_at: string;
  resolved_at: string | null;
}
export interface CaseTimelineEvent {
  id: string;
  case_id: string;
  kind: string;
  actor: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}
export interface CaseListResp {
  cases: CaseFile[];
  summary: { total: number; open: number };
}
export interface CaseDetail {
  case: CaseFile;
  timeline: CaseTimelineEvent[];
}
export interface CaseCreateBody {
  title: string;
  summary?: string;
  severity?: string;
  workload_id?: string | null;
  workload_name?: string | null;
  connection_id?: string | null;
  architecture_id?: string | null;
  investigation_chat_id?: string | null;
  investigation_message_id?: string | null;
  finding_uids?: string[];
  change_event_ids?: string[];
  risk_score?: number | null;
  confidence?: number | null;
  assignee?: string | null;
}
export interface CaseUpdateBody {
  title?: string;
  summary?: string;
  status?: CaseStatus;
  severity?: string;
  risk_score?: number | null;
  confidence?: number | null;
  assignee?: string | null;
  remediation_task_id?: string | null;
  verification_json?: Record<string, unknown> | null;
}

export const api = {
  me: () => http<Me>("/me"),
  activeLlm: () => http<ActiveLlm>("/llm/active"),
  // Connection capability & blind-spot matrix. live=true verifies ARM/Graph tokens for real.
  capabilityMatrix: (live = false) =>
    http<CapabilityMatrix>(`/capability/matrix?live=${live ? "true" : "false"}`),
  listChats: () => http<Chat[]>("/chats"),
  // Server source of truth for chats with an in-flight turn (cross-tab spinners).
  activeTurns: () => http<{ active: string[] }>("/chats/active"),
  // Stop the in-flight agent turn for a chat (cancels the server-side background task).
  stopTurn: (id: string) =>
    http<{ stopped: boolean }>(`/chats/${id}/stop`, { method: "POST", body: "{}" }),
  // Recent deep investigations across the caller's chats (history + confidence).
  deepInvestigations: (limit = 30) =>
    http<{ investigations: DeepInvestigationSummary[] }>(`/chats/investigations?limit=${limit}`),
  // Save an investigation's root-cause analysis into the linked architecture's Memory
  // ("Case Law"). Pass architectureId to disambiguate when several are linked.
  saveInvestigationRca: (messageId: string, architectureId?: string) =>
    http<SaveRcaResult>(`/chats/investigations/${messageId}/save-rca`, {
      method: "POST",
      body: JSON.stringify({ architecture_id: architectureId ?? null }),
    }),
  // Durable Case Files — the persistent spine of an incident.
  cases: (opts?: { status?: string; workloadId?: string; openOnly?: boolean }) => {
    const p = new URLSearchParams();
    if (opts?.status) p.set("status", opts.status);
    if (opts?.workloadId) p.set("workload_id", opts.workloadId);
    if (opts?.openOnly) p.set("open_only", "true");
    const qs = p.toString();
    return http<CaseListResp>(`/cases${qs ? `?${qs}` : ""}`);
  },
  caseMeta: () => http<{ statuses: CaseStatus[]; severities: string[] }>("/cases/meta"),
  createCase: (body: CaseCreateBody) =>
    http<CaseFile>("/cases", { method: "POST", body: JSON.stringify(body) }),
  getCase: (id: string) => http<CaseDetail>(`/cases/${id}`),
  updateCase: (id: string, body: CaseUpdateBody) =>
    http<CaseFile>(`/cases/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  attachToCase: (id: string, field: string, values: string[], label?: string) =>
    http<CaseFile>(`/cases/${id}/attach`, {
      method: "POST",
      body: JSON.stringify({ field, values, label }),
    }),
  addCaseNote: (id: string, message: string, kind = "note") =>
    http<CaseTimelineEvent>(`/cases/${id}/events`, {
      method: "POST",
      body: JSON.stringify({ message, kind }),
    }),
  deleteCase: (id: string) =>
    http<{ deleted: boolean }>(`/cases/${id}`, { method: "DELETE" }),
  createChat: () => http<Chat>("/chats", { method: "POST", body: "{}" }),
  deleteChat: (id: string) => http<{ ok: boolean }>(`/chats/${id}`, { method: "DELETE" }),
  deleteAllChats: () => http<{ ok: boolean; deleted: number }>("/chats", { method: "DELETE" }),
  // Trash (soft-deleted / archived chats).
  trashChats: () => http<Chat[]>("/chats/trash"),
  restoreChat: (id: string) =>
    http<Chat>(`/chats/${id}/restore`, { method: "POST", body: "{}" }),
  purgeChat: (id: string) =>
    http<{ ok: boolean }>(`/chats/${id}/purge`, { method: "DELETE" }),
  emptyTrash: () =>
    http<{ ok: boolean; deleted: number }>("/chats/trash/empty", { method: "POST", body: "{}" }),
  listMessages: (id: string) => http<Message[]>(`/chats/${id}/messages`),
  ticketConnectors: () => http<{ connectors: TicketConnector[] }>("/chats/ticket-connectors"),
  sendChatToTicket: (id: string, connectorId: string) =>
    http<TicketResult>(`/chats/${id}/send-ticket`, {
      method: "POST",
      body: JSON.stringify({ connector_id: connectorId }),
    }),
  breakoutChat: (id: string, upToMessageId?: string) =>
    http<Chat>(`/chats/${id}/breakout`, {
      method: "POST",
      body: JSON.stringify({ up_to_message_id: upToMessageId ?? null }),
    }),
  deleteMessagesFrom: (id: string, messageId: string) =>
    http<{ ok: boolean; deleted: number }>(`/chats/${id}/messages/from/${messageId}`, {
      method: "DELETE",
    }),
  suggestions: (id: string) =>
    http<{ suggestions: string[] }>(`/chats/${id}/suggestions`),
  clarify: (id: string, content: string) =>
    http<{
      needs_subscription: boolean;
      options: SubscriptionOption[];
      needs_management_group: boolean;
      mg_options: ManagementGroupOption[];
    }>(
      `/chats/${id}/clarify`,
      { method: "POST", body: JSON.stringify({ content }) },
    ),
  proposeProblems: (id: string, content: string, candidates: string[]) =>
    http<{ suggestions: string[] }>(
      `/chats/${id}/propose`,
      { method: "POST", body: JSON.stringify({ content, candidates }) },
    ),
  deepSuggestAgents: (id: string, content: string) =>
    http<{ agents: DeepAgent[] }>(
      `/chats/${id}/deep/agents`,
      { method: "POST", body: JSON.stringify({ content }) },
    ),
  listTools: () =>
    http<{ name: string; description: string; kind: string }[]>("/admin/mcp/tools"),
  listEntraTools: () =>
    http<{
      enabled: boolean;
      connection_configured: boolean;
      tools: { name: string; description: string; kind: string }[];
    }>("/admin/entra/tools"),
  listBuiltinTools: () =>
    http<{
      enabled: boolean;
      disabled: string[];
      egress_denylist: string[];
      egress_allowlist: string[];
      tools: { name: string; description: string; kind: string; active: boolean }[];
    }>("/admin/builtin/tools"),
  usage: () =>
    http<{
      provider: string;
      model: string;
      requests: number;
      prompt_tokens: number;
      completion_tokens: number;
      cost_usd: number;
      estimated: boolean;
    }[]>("/admin/usage"),
  audit: (limit = 25, offset = 0) =>
    http<AuditPage>(`/admin/audit?limit=${limit}&offset=${offset}`),
  siemExport: () => http<SiemDestinationsResponse>("/admin/siem-export"),
  addSiemDestination: (body: Partial<SiemDestinationInput>) =>
    http<SiemDestinationsResponse>("/admin/siem-export", { method: "POST", body: JSON.stringify(body) }),
  updateSiemDestination: (id: string, body: Partial<SiemDestinationInput>) =>
    http<SiemDestinationsResponse>(`/admin/siem-export/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteSiemDestination: (id: string) =>
    http<SiemDestinationsResponse>(`/admin/siem-export/${id}`, { method: "DELETE" }),
  testSiemDestination: (id: string) =>
    http<{ ok: boolean; error: string | null }>(`/admin/siem-export/${id}/test`, {
      method: "POST",
      body: "{}",
    }),
  flushSiemDestination: (id: string) =>
    http<{ forwarded: number; error: string | null }>(`/admin/siem-export/${id}/flush`, {
      method: "POST",
      body: "{}",
    }),
  resetSiemCursor: (id: string) =>
    http<SiemDestinationsResponse>(`/admin/siem-export/${id}/reset-cursor`, {
      method: "POST",
      body: "{}",
    }),
  monitor: (days?: number, workloadId?: string) =>
    http<MonitorOverview>(
      `/admin/monitor${(() => {
        const p = new URLSearchParams();
        if (days) p.set("days", String(days));
        if (workloadId) p.set("workload_id", workloadId);
        const qs = p.toString();
        return qs ? `?${qs}` : "";
      })()}`,
    ),
  monitorDashboards: () =>
    http<{ dashboards: MonitorDashboard[] }>("/admin/monitor/dashboards"),
  upsertMonitorDashboard: (body: Partial<MonitorDashboard>) =>
    http<{ dashboard: MonitorDashboard }>("/admin/monitor/dashboards", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteMonitorDashboard: (id: string) =>
    http<{ ok: boolean }>(`/admin/monitor/dashboards/${id}`, { method: "DELETE" }),
  setDefaultMonitorDashboard: (id: string) =>
    http<{ dashboard: MonitorDashboard }>(`/admin/monitor/dashboards/${id}/set-default`, {
      method: "POST",
      body: "{}",
    }),
  // ---- Monitor 2.0: data-bound widgets + AI authoring ----
  monitorDatasources: () =>
    http<{ datasources: MonitorDatasourceDef[]; widgets: MonitorWidgetTypeDef[] }>(
      "/admin/monitor/datasources",
    ),
  runMonitorWidget: (dataSource: MonitorDataSource, params?: Record<string, unknown>, noCache?: boolean) =>
    http<{ result: WidgetTableResult }>("/admin/monitor/widgets/run", {
      method: "POST",
      body: JSON.stringify({ dataSource, params: params ?? {}, no_cache: !!noCache }),
    }),
  // Fetch the time-series behind a chat ```azchart block by its id (see MetricChart).
  getChartArtifact: (chartId: string) =>
    http<ChartArtifact>(`/charts/${encodeURIComponent(chartId)}`),
  aiBuildWidget: (prompt: string) =>
    http<{ widget: MonitorWidget }>("/admin/monitor/ai/widget", {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  aiSuggestDashboard: (workloadId: string, archetype?: string) =>
    http<MonitorDashboardSuggestion>("/admin/monitor/ai/dashboard/suggest", {
      method: "POST",
      body: JSON.stringify({ workload_id: workloadId, archetype: archetype ?? "full_stack" }),
    }),
  aiBuildDashboard: (workloadId: string, selected?: unknown[], save?: boolean, archetype?: string) =>
    http<MonitorDashboardBuildResult>("/admin/monitor/ai/dashboard", {
      method: "POST",
      body: JSON.stringify({ workload_id: workloadId, selected, save: !!save, archetype: archetype ?? "full_stack" }),
    }),
  monitorDashboardVersions: (id: string) =>
    http<{ versions: MonitorDashboardRevision[] }>(`/admin/monitor/dashboards/${id}/versions`),
  restoreMonitorDashboard: (id: string, version: number) =>
    http<{ dashboard: MonitorDashboard }>(`/admin/monitor/dashboards/${id}/restore/${version}`, {
      method: "POST",
      body: "{}",
    }),
  llmConfig: () => http<LLMConfig>("/admin/llm/config"),
  updateLlmConfig: (body: LLMConfigUpdate) =>
    http<LLMConfig>("/admin/llm/config", { method: "PUT", body: JSON.stringify(body) }),
  llmModels: (provider: string, freeOnly?: boolean, includeHidden?: boolean) =>
    http<{ models: string[] }>(
      `/admin/llm/models?provider=${encodeURIComponent(provider)}` +
        (freeOnly !== undefined ? `&free_only=${freeOnly}` : "") +
        (includeHidden ? `&include_hidden=true` : ""),
    ),
  testLlmProvider: (provider: string) =>
    http<{ ok: boolean; detail: string }>("/admin/llm/test", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),
  importChatgptOauth: () =>
    http<{ ok: boolean; account_id: string }>("/admin/llm/oauth/chatgpt/import", {
      method: "POST",
      body: "{}",
    }),
  importGithubCopilotOauth: () =>
    http<{ ok: boolean }>("/admin/llm/oauth/github/import", {
      method: "POST",
      body: "{}",
    }),
  // GitHub OAuth device flow (headless / remote sign-in — no server browser).
  githubCopilotDeviceStart: () =>
    http<{ ok: boolean; user_code: string; verification_uri: string; expires_in: number; interval: number }>(
      "/admin/llm/oauth/github/device/start",
      { method: "POST", body: "{}" },
    ),
  githubCopilotDevicePoll: () =>
    http<{ ok: boolean; status: { status: string; detail?: string } & Partial<GithubCopilotStatus> }>(
      "/admin/llm/oauth/github/device/poll",
      { method: "POST", body: "{}" },
    ),
  githubCopilotRefresh: () =>
    http<{ ok: boolean; status: GithubCopilotStatus }>("/admin/llm/oauth/github/refresh", {
      method: "POST",
      body: "{}",
    }),
  githubCopilotSignout: () =>
    http<{ ok: boolean; status: GithubCopilotStatus }>("/admin/llm/oauth/github/signout", {
      method: "POST",
      body: "{}",
    }),
  githubCopilotStatus: () =>
    http<GithubCopilotStatus>("/admin/llm/oauth/github/status"),
  chatgptRefresh: () =>
    http<{ ok: boolean; status: ChatgptStatus }>("/admin/llm/oauth/chatgpt/refresh", {
      method: "POST",
      body: "{}",
    }),
  chatgptAuthorizeUrl: () =>
    http<{ ok: boolean; authorize_url: string; state: string }>(
      "/admin/llm/oauth/chatgpt/authorize-url",
      { method: "POST", body: "{}" },
    ),
  chatgptComplete: (callbackUrl: string) =>
    http<{ ok: boolean; status: ChatgptStatus }>("/admin/llm/oauth/chatgpt/complete", {
      method: "POST",
      body: JSON.stringify({ callback_url: callbackUrl }),
    }),
  chatgptSignout: () =>
    http<{ ok: boolean; status: ChatgptStatus }>("/admin/llm/oauth/chatgpt/signout", {
      method: "POST",
      body: "{}",
    }),
  chatgptStatus: () => http<ChatgptStatus>("/admin/llm/oauth/chatgpt/status"),
  claudeRefresh: () =>
    http<{ ok: boolean; status: ClaudeOauthStatus }>("/admin/llm/oauth/claude/refresh", {
      method: "POST",
      body: "{}",
    }),
  claudeAuthorizeUrl: () =>
    http<{ ok: boolean; authorize_url: string; state: string }>(
      "/admin/llm/oauth/claude/authorize-url",
      { method: "POST", body: "{}" },
    ),
  claudeComplete: (callbackUrl: string) =>
    http<{ ok: boolean; status: ClaudeOauthStatus }>("/admin/llm/oauth/claude/complete", {
      method: "POST",
      body: JSON.stringify({ callback_url: callbackUrl }),
    }),
  claudeSignout: () =>
    http<{ ok: boolean; status: ClaudeOauthStatus }>("/admin/llm/oauth/claude/signout", {
      method: "POST",
      body: "{}",
    }),
  claudeStatus: () => http<ClaudeOauthStatus>("/admin/llm/oauth/claude/status"),
  appSettings: () =>
    http<{ settings: AppSettings; response_styles: string[]; command_binaries?: string[] }>(
      "/admin/settings",
    ),
  updateAppSettings: (body: Partial<AppSettings>) =>
    http<{ settings: AppSettings }>("/admin/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  // Demo data (Settings → Demo Data): load/remove the demo dataset across all features.
  demoStatus: () =>
    http<DemoStatus>("/admin/demo/status"),
  seedDemoData: () =>
    http<DemoResult>("/admin/demo/seed", { method: "POST", body: "{}" }),
  purgeDemoData: () =>
    http<DemoResult>("/admin/demo/purge", { method: "POST", body: "{}" }),
  // App metadata + health (About dialog + System Status panel).
  meta: () => http<AppMeta>("/meta"),
  metaStatus: () => http<AppStatus>("/meta/status"),
  aiPrompts: () => http<{ prompts: AiPrompt[] }>("/admin/ai-prompts"),
  updateAiPrompts: (values: Record<string, string>) =>
    http<{ prompts: AiPrompt[] }>("/admin/ai-prompts", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),
  resetAiPrompt: (id: string) =>
    http<{ prompts: AiPrompt[] }>(`/admin/ai-prompts/${id}/reset`, {
      method: "POST",
      body: "{}",
    }),
  renameChat: (id: string, title: string) =>
    http<Chat>(`/chats/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  pinChat: (id: string, pinned: boolean) =>
    http<Chat>(`/chats/${id}`, { method: "PATCH", body: JSON.stringify({ pinned }) }),
  setChatModel: (id: string, provider: string, model: string) =>
    http<Chat>(`/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ provider, model }),
    }),
  setChatConnection: (id: string, connectionId: string | null) =>
    http<Chat>(`/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ connection_id: connectionId ?? "" }),
    }),
  setChatThinking: (id: string, level: "normal" | "deep") =>
    http<Chat>(`/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ thinking_level: level }),
    }),
  setChatAgent: (id: string, agentId: string | null) =>
    http<Chat>(`/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ agent_id: agentId ?? "" }),
    }),
  // --- Azure connections (tenants) ---
  azureConnections: () =>
    http<{ connections: TenantOption[] }>("/azure/connections"),
  adminConnections: () =>
    http<{ connections: AzureConnection[]; auth_methods: string[] }>(
      "/admin/connections",
    ),
  upsertConnection: (body: ConnectionUpsert) =>
    http<{ connection: AzureConnection }>("/admin/connections", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteConnection: (id: string) =>
    http<{ ok: boolean }>(`/admin/connections/${id}`, { method: "DELETE" }),
  setDefaultConnection: (id: string) =>
    http<{ ok: boolean; connections: AzureConnection[] }>(
      `/admin/connections/${id}/default`,
      { method: "POST", body: "{}" },
    ),
  testConnection: (id: string) =>
    http<{
      ok: boolean;
      detail?: string;
      subscription_count?: number;
      subscriptions?: { id: string; name: string; state?: string }[];
      connection?: AzureConnection;
    }>(`/admin/connections/${id}/test`, { method: "POST", body: "{}" }),
  discoverConnection: (id: string) =>
    http<{
      ok: boolean;
      detail?: string;
      subscriptions: { id: string; name: string; state?: string; is_default?: boolean }[];
      management_groups: { id: string; name: string }[];
    }>(`/admin/connections/${id}/discover`),
  validateEntra: (id: string) =>
    http<{
      ok: boolean;
      detail?: string;
      report?: EntraValidation;
    }>(`/admin/connections/${id}/validate-entra`, { method: "POST", body: "{}" }),

  // --- Sandbox troubleshooting VMs ---
  sandboxVms: () =>
    http<{ vms: SandboxVm[]; auth_methods: string[] }>("/admin/sandbox-vms"),
  upsertSandboxVm: (body: SandboxVmUpsert) =>
    http<{ vm: SandboxVm }>("/admin/sandbox-vms", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteSandboxVm: (id: string) =>
    http<{ ok: boolean }>(`/admin/sandbox-vms/${id}`, { method: "DELETE" }),
  testSandboxVm: (id: string) =>
    http<{
      ok: boolean;
      detail?: string;
      whoami?: string;
      os_info?: string;
      capabilities?: string[];
      pkg_manager?: string;
      can_sudo?: boolean;
      sudo_mode?: string;
      fingerprint?: string;
    }>(`/admin/sandbox-vms/${id}/test`, { method: "POST", body: "{}" }),
  runSandboxVm: (id: string, command: string, confirm = false) =>
    http<{
      ok: boolean;
      needs_approval: boolean;
      destructive: boolean;
      exit_code: number | null;
      stdout: string;
      stderr: string;
      error: string;
      duration_ms: number | null;
    }>(`/admin/sandbox-vms/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ command, confirm }),
    }),
  sandboxVmRuns: (vmId?: string) =>
    http<{ runs: SandboxVmRun[] }>(
      "/admin/sandbox-vms/runs" + (vmId ? `?vm_id=${encodeURIComponent(vmId)}` : ""),
    ),

  // --- Connectors (Teams/Outlook/Jira/Grafana) ---
  connectors: () =>
    http<{ connectors: AppConnector[]; types: ConnectorTypeMeta[] }>("/admin/connectors"),
  upsertConnector: (body: ConnectorUpsert) =>
    http<{ connector: AppConnector }>("/admin/connectors", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteConnector: (id: string) =>
    http<{ ok: boolean }>(`/admin/connectors/${id}`, { method: "DELETE" }),
  testConnector: (id: string) =>
    http<{ ok: boolean; detail?: string; connector?: AppConnector }>(
      `/admin/connectors/${id}/test`,
      { method: "POST", body: "{}" },
    ),
  sendTestConnectorMessage: (id: string) =>
    http<{ ok: boolean; detail?: string; connector?: AppConnector }>(
      `/admin/connectors/${id}/send-test`,
      { method: "POST", body: "{}" },
    ),

  // --- Custom agents ---
  customAgents: () =>
    http<{ agents: CustomAgent[]; tools: ConnectorToolInfo[]; categories: AgentCategory[] }>("/admin/automations/agents"),
  upsertAgent: (body: Partial<CustomAgent>) =>
    http<{ agent: CustomAgent }>("/admin/automations/agents", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  setAgentEnabled: (id: string, name: string, enabled: boolean) =>
    http<{ agent: CustomAgent }>("/admin/automations/agents", {
      method: "PUT",
      body: JSON.stringify({ id, name, enabled }),
    }),
  deleteAgent: (id: string) =>
    http<{ ok: boolean }>(`/admin/automations/agents/${id}`, { method: "DELETE" }),
  // Export agent config(s) as portable JSON (single or bulk; empty ids = all).
  exportAgent: (id: string) =>
    http<AgentExport>(`/admin/automations/agents/${id}/export`),
  exportAgents: (ids: string[] = []) =>
    http<AgentBundleExport>("/admin/automations/agents/export", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  importAgents: (data: unknown, overwriteExisting = true) =>
    http<{ created: number; updated: number; agents: { id: string; name: string }[] }>(
      "/admin/automations/agents/import",
      {
        method: "POST",
        body: JSON.stringify({ data, overwrite_existing: overwriteExisting }),
      },
    ),
  // Enhance an EXISTING agent with AI (assess → interview → enhanced draft).
  enhanceInterview: (agentId: string, answers: AgentAnswer[], step: number) =>
    http<AgentEnhanceInterview>(
      `/admin/automations/agents/${agentId}/enhance/interview`,
      {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, answers, step }),
      },
    ),
  enhanceGenerate: (agentId: string, answers: AgentAnswer[]) =>
    http<AgentEnhanceResult>(
      `/admin/automations/agents/${agentId}/enhance/generate`,
      {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, answers }),
      },
    ),
  // AI-guided agent designer (wizard).
  agentInterview: (goal: string, answers: AgentAnswer[], step: number) =>
    http<AgentInterviewResult>("/admin/automations/agents/draft/interview", {
      method: "POST",
      body: JSON.stringify({ goal, answers, step }),
    }),
  agentGenerate: (goal: string, answers: AgentAnswer[]) =>
    http<{ draft: AgentDraft }>("/admin/automations/agents/draft/generate", {
      method: "POST",
      body: JSON.stringify({ goal, answers }),
    }),

  // AI-guided Workbook designer.
  workbookInterview: (goal: string, answers: AgentAnswer[], step: number) =>
    http<AgentInterviewResult>("/workbooks/draft/interview", {
      method: "POST",
      body: JSON.stringify({ goal, answers, step }),
    }),
  workbookGenerate: (goal: string, answers: AgentAnswer[]) =>
    http<{ draft: WorkbookDraft }>("/workbooks/draft/generate", {
      method: "POST",
      body: JSON.stringify({ goal, answers }),
    }),
  workbookEnhanceInterview: (workbookId: string, answers: AgentAnswer[], step: number) =>
    http<AgentInterviewResult & { assessment?: string }>(`/workbooks/${workbookId}/enhance/interview`, {
      method: "POST",
      body: JSON.stringify({ answers, step }),
    }),
  workbookEnhanceGenerate: (workbookId: string, answers: AgentAnswer[]) =>
    http<{ draft: WorkbookDraft; current: Partial<Workbook> }>(`/workbooks/${workbookId}/enhance/generate`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    }),

  // AI-guided Playbook designer.
  playbookInterview: (goal: string, answers: AgentAnswer[], step: number) =>
    http<AgentInterviewResult>("/playbooks/draft/interview", {
      method: "POST",
      body: JSON.stringify({ goal, answers, step }),
    }),
  playbookGenerate: (goal: string, answers: AgentAnswer[]) =>
    http<{ draft: PlaybookDraft }>("/playbooks/draft/generate", {
      method: "POST",
      body: JSON.stringify({ goal, answers }),
    }),

  // --- AI Insight Packs ---
  insightPacks: () => http<InsightPackLibrary>("/insights/packs"),
  insightTemplates: () => http<{ templates: InsightPack[] }>("/insights/templates"),
  insightPack: (id: string) => http<{ pack: InsightPack; markdown: string }>(`/insights/packs/${id}`),
  upsertInsightPack: (pack: Partial<InsightPack>) =>
    http<{ pack: InsightPack }>("/insights/packs", { method: "PUT", body: JSON.stringify(pack) }),
  deleteInsightPack: (id: string) =>
    http<{ ok: boolean }>(`/insights/packs/${id}`, { method: "DELETE" }),
  setInsightPackEnabled: (id: string, enabled: boolean) =>
    http<{ pack: InsightPack }>(`/insights/packs/${id}/enable`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  cloneInsightPack: (id: string) =>
    http<{ pack: InsightPack }>(`/insights/packs/${id}/clone`, { method: "POST" }),
  insightInterview: (goal: string, answers: AgentAnswer[], step: number) =>
    http<InsightInterviewResult>("/insights/draft/interview", {
      method: "POST",
      body: JSON.stringify({ goal, answers, step }),
    }),
  insightGenerate: (goal: string, answers: AgentAnswer[]) =>
    http<{ draft: InsightPack; summary: string }>("/insights/draft/generate", {
      method: "POST",
      body: JSON.stringify({ goal, answers }),
    }),
  insightPreview: (goal: string, answers: AgentAnswer[]) =>
    http<InsightPackPreview>("/insights/draft/preview", {
      method: "POST",
      body: JSON.stringify({ goal, answers }),
    }),
  refineInsightPack: (pack: Partial<InsightPack>, instruction: string, mode: InsightRefineMode) =>
    http<InsightRefineResult>("/insights/draft/refine", {
      method: "POST",
      body: JSON.stringify({ pack, instruction, mode }),
    }),
  runInsightPack: (body: {
    pack_id?: string;
    pack?: Partial<InsightPack>;
    scope: InsightScope;
    overrides?: Record<string, unknown>;
    notify?: boolean;
  }) => http<{ run: InsightRun }>("/insights/run", { method: "POST", body: JSON.stringify(body) }),
  startInsightRun: (body: {
    pack_id?: string;
    pack?: Partial<InsightPack>;
    scope: InsightScope;
    overrides?: Record<string, unknown>;
    notify?: boolean;
  }) => http<{ job_id: string }>("/insights/run/async", { method: "POST", body: JSON.stringify(body) }),
  getInsightRunJob: (jobId: string) =>
    http<{ job: InsightRunJob }>(`/insights/run/jobs/${encodeURIComponent(jobId)}`),
  insightRuns: (packId?: string, limit = 100) =>
    http<{ runs: InsightRun[] }>(
      `/insights/runs?limit=${limit}${packId ? `&pack_id=${encodeURIComponent(packId)}` : ""}`,
    ),
  insightLatest: () => http<{ latest: InsightRun[] }>("/insights/latest"),
  insightRun: (id: string) => http<{ run: InsightRun }>(`/insights/runs/${id}`),
  setInsightRunState: (
    id: string,
    body: { read?: boolean; acknowledged?: boolean; false_positive?: boolean },
  ) =>
    http<{ run: InsightRun }>(`/insights/runs/${id}/state`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  markAllInsightRunsRead: () =>
    http<{ updated: number }>("/insights/runs/read-all", { method: "POST" }),
  insightUpcoming: (days = 7) =>
    http<{ days: number; occurrences: InsightOccurrence[] }>(`/insights/schedule/upcoming?days=${days}`),
  insightCoverage: (workloadId?: string, days = 7) =>
    http<InsightCoverage>(
      `/insights/coverage?days=${days}${workloadId ? `&workload_id=${encodeURIComponent(workloadId)}` : ""}`,
    ),
  insightHealth: () => http<{ health: Record<string, InsightPackHealth> }>("/insights/health"),
  snoozeInsightPack: (id: string, days: number) =>
    http<{ pack: InsightPack }>(`/insights/packs/${id}/snooze`, {
      method: "POST",
      body: JSON.stringify({ days }),
    }),
  pinInsightPack: (id: string, pinned: boolean) =>
    http<{ pack: InsightPack }>(`/insights/packs/${id}/pin`, {
      method: "POST",
      body: JSON.stringify({ pinned }),
    }),
  setInsightPackCollections: (id: string, collection_ids: string[]) =>
    http<{ pack: InsightPack }>(`/insights/packs/${id}/collections`, {
      method: "POST",
      body: JSON.stringify({ collection_ids }),
    }),
  createInsightCollection: (name: string, icon = "📁") =>
    http<{ collection: InsightCollection }>("/insights/collections", {
      method: "POST",
      body: JSON.stringify({ name, icon }),
    }),
  updateInsightCollection: (id: string, body: { name?: string; icon?: string }) =>
    http<{ collection: InsightCollection }>(`/insights/collections/${id}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteInsightCollection: (id: string) =>
    http<{ ok: boolean }>(`/insights/collections/${id}`, { method: "DELETE" }),
  insightRunPdf: (id: string) => httpBlob(`/insights/runs/${id}/pdf`),

  // --- Scheduled tasks ---
  scheduledTasks: () =>
    http<{ tasks: ScheduledTask[]; metrics: TaskMetrics }>("/admin/automations/tasks"),
  previewSchedule: (body: {
    schedule_kind: string;
    cron_expr?: string | null;
    time_of_day?: string | null;
    weekday?: number | null;
    timezone?: string;
  }) =>
    http<{ valid: boolean; error: string | null; next_run_at: string | null; next_runs: string[]; schedule_label: string | null }>(
      "/admin/automations/tasks/preview",
      { method: "POST", body: JSON.stringify(body) },
    ),
  upsertTask: (body: Partial<ScheduledTask>) =>
    http<{ task: ScheduledTask }>("/admin/automations/tasks", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  toggleTask: (id: string) =>
    http<{ task: ScheduledTask }>(`/admin/automations/tasks/${id}/toggle`, {
      method: "POST",
      body: "{}",
    }),
  deleteTask: (id: string) =>
    http<{ ok: boolean }>(`/admin/automations/tasks/${id}`, { method: "DELETE" }),
  archivedTasks: () =>
    http<{ tasks: ScheduledTask[] }>("/admin/automations/tasks/archived"),
  restoreTask: (id: string) =>
    http<{ task: ScheduledTask }>(`/admin/automations/tasks/${id}/restore`, {
      method: "POST",
      body: "{}",
    }),
  purgeTask: (id: string) =>
    http<{ ok: boolean }>(`/admin/automations/tasks/${id}/purge`, { method: "DELETE" }),
  runTaskNow: (id: string) =>
    http<{ ok: boolean; message: string }>(`/admin/automations/tasks/${id}/run`, {
      method: "POST",
      body: "{}",
    }),
  taskRuns: (id: string) =>
    http<{ runs: TaskRunInfo[] }>(`/admin/automations/tasks/${id}/runs`),

  // --- Backup & Restore (whole-tenant) ---
  backupSections: () =>
    http<{ sections: BackupSection[] }>("/admin/backup/sections"),
  backupExport: (sections: string[], include_chats = false) =>
    httpBlob("/admin/backup/export", {
      method: "POST",
      body: JSON.stringify({ sections, include_chats }),
    }),
  backupImportPreview: (data: unknown, mode: BackupConflictMode) =>
    http<BackupImportPreview>("/admin/backup/import/preview", {
      method: "POST",
      body: JSON.stringify({ data, mode }),
    }),
  backupImport: (data: unknown, mode: BackupConflictMode, sections: string[]) =>
    http<BackupImportResult>("/admin/backup/import", {
      method: "POST",
      body: JSON.stringify({ data, mode, sections }),
    }),

  // --- Authentication (local + SSO) ---
  authConfig: () => http<AuthConfig>("/auth/config"),
  authMe: () => http<Me>("/auth/me"),
  login: (username: string, password: string) =>
    http<{ ok: boolean; user: Me }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => http<{ ok: boolean }>("/auth/logout", { method: "POST", body: "{}" }),
  setActiveRole: (role: string) =>
    http<{ ok: boolean; active_role: string }>("/auth/active-role", {
      method: "POST",
      body: JSON.stringify({ role }),
    }),
  updateProfile: (body: { first_name?: string; last_name?: string; language?: string; default_role?: string }) =>
    http<{ ok: boolean }>("/auth/profile", { method: "PATCH", body: JSON.stringify(body) }),
  changePassword: (newPassword: string, currentPassword?: string) =>
    http<{ ok: boolean }>("/auth/change-password", {
      method: "POST",
      body: JSON.stringify({
        new_password: newPassword,
        current_password: currentPassword ?? null,
      }),
    }),

  // --- Access control (admin: users / roles / groups / IdPs / sessions / policies) ---
  acUsers: () => http<AcUser[]>("/admin/access/users"),
  acCreateUser: (body: AcUserCreate) =>
    http<AcUser>("/admin/access/users", { method: "POST", body: JSON.stringify(body) }),
  acUpdateUser: (id: string, body: AcUserUpdate) =>
    http<AcUser>(`/admin/access/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  acDeleteUser: (id: string) =>
    http<{ ok: boolean }>(`/admin/access/users/${id}`, { method: "DELETE" }),
  acResetPassword: (id: string, newPassword: string, mustChange: boolean) =>
    http<{ ok: boolean }>(`/admin/access/users/${id}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword, must_change_password: mustChange }),
    }),
  acRevokeUserSessions: (id: string) =>
    http<{ ok: boolean; revoked: number }>(`/admin/access/users/${id}/revoke-sessions`, {
      method: "POST",
      body: "{}",
    }),
  acPermissions: () => http<{ key: string; label: string; group?: string }[]>("/admin/access/permissions"),
  acRoles: () => http<AcRole[]>("/admin/access/roles"),
  acCreateRole: (body: AcRoleBody) =>
    http<AcRole>("/admin/access/roles", { method: "POST", body: JSON.stringify(body) }),
  acUpdateRole: (id: string, body: AcRoleBody) =>
    http<AcRole>(`/admin/access/roles/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  acDeleteRole: (id: string) =>
    http<{ ok: boolean }>(`/admin/access/roles/${id}`, { method: "DELETE" }),
  acGroups: () => http<AcGroup[]>("/admin/access/groups"),
  acCreateGroup: (body: AcGroupBody) =>
    http<AcGroup>("/admin/access/groups", { method: "POST", body: JSON.stringify(body) }),
  acUpdateGroup: (id: string, body: AcGroupBody) =>
    http<AcGroup>(`/admin/access/groups/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  acDeleteGroup: (id: string) =>
    http<{ ok: boolean }>(`/admin/access/groups/${id}`, { method: "DELETE" }),
  acIdps: () => http<AcIdp[]>("/admin/access/identity-providers"),
  acCreateIdp: (body: AcIdpBody) =>
    http<AcIdp>("/admin/access/identity-providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  acUpdateIdp: (id: string, body: AcIdpBody) =>
    http<AcIdp>(`/admin/access/identity-providers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  acTestIdp: (body: AcIdpBody, id?: string) =>
    http<IdpTestResult>(`/admin/access/identity-providers/test${id ? `?idp_id=${encodeURIComponent(id)}` : ""}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  acDeleteIdp: (id: string) =>
    http<{ ok: boolean }>(`/admin/access/identity-providers/${id}`, { method: "DELETE" }),
  acSessions: (includeExpired = false) =>
    http<{ sessions: AcSession[]; expired_count: number }>(
      `/admin/access/sessions${includeExpired ? "?include_expired=true" : ""}`,
    ),
  acRevokeSession: (id: string) =>
    http<{ ok: boolean }>(`/admin/access/sessions/${id}`, { method: "DELETE" }),
  acRevokeExpiredSessions: () =>
    http<{ ok: boolean; revoked: number }>("/admin/access/sessions/revoke-expired", {
      method: "POST",
      body: "{}",
    }),
  acPolicies: () =>
    http<{ values: AuthPolicies; defaults: AuthPolicies }>("/admin/access/policies"),
  acUpdatePolicies: (body: Partial<AuthPolicies>) =>
    http<AuthPolicies>("/admin/access/policies", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // --- Workbooks (az / KQL / PowerShell snippets, AI'fied) ---
  workbooks: () => http<{ workbooks: Workbook[] }>("/workbooks"),
  upsertWorkbook: (body: Partial<Workbook>) =>
    http<{ workbook: Workbook }>("/workbooks", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteWorkbook: (id: string) =>
    http<{ ok: boolean }>(`/workbooks/${id}`, { method: "DELETE" }),
  exportWorkbook: (id: string) => http<BundleEnvelope>(`/workbooks/${id}/export`),
  importWorkbook: (bundle: unknown) =>
    http<{ workbook: Workbook }>("/workbooks/import", {
      method: "POST",
      body: JSON.stringify({ bundle }),
    }),
  runWorkbook: (id: string, body: WorkbookRunRequest) =>
    http<{ run: WorkbookRun }>(`/workbooks/${id}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  previewWorkbook: (body: WorkbookPreviewRequest) =>
    http<{ run: WorkbookRun }>(`/workbooks/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  workbookRuns: (workbookId?: string) =>
    http<{ runs: WorkbookRun[] }>(
      `/workbooks/runs${workbookId ? `?workbook_id=${encodeURIComponent(workbookId)}` : ""}`,
    ),
  workbookTiles: () => http<{ tiles: WorkbookTile[] }>("/workbooks/tiles"),

  // --- Playbooks (chained workbooks) ---
  playbooks: () => http<{ playbooks: Playbook[] }>("/playbooks"),
  upsertPlaybook: (body: Partial<Playbook>) =>
    http<{ playbook: Playbook }>("/playbooks", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deletePlaybook: (id: string) =>
    http<{ ok: boolean }>(`/playbooks/${id}`, { method: "DELETE" }),
  exportPlaybook: (id: string) => http<BundleEnvelope>(`/playbooks/${id}/export`),
  importPlaybook: (bundle: unknown) =>
    http<{ playbook: Playbook; workbooks_imported: number; steps_dropped: number }>(
      "/playbooks/import",
      { method: "POST", body: JSON.stringify({ bundle }) },
    ),
  runPlaybook: (id: string) =>
    http<{ result: PlaybookRunResult }>(`/playbooks/${id}/run`, {
      method: "POST",
      body: "{}",
    }),
  playbookRuns: (playbookId?: string) =>
    http<{ runs: PlaybookRun[] }>(
      `/playbooks/runs${playbookId ? `?playbook_id=${encodeURIComponent(playbookId)}` : ""}`,
    ),

  // --- Notifications (in-app center + global rules) ---
  notifications: (unreadOnly = false) =>
    http<{ notifications: AppNotification[] }>(
      `/notifications${unreadOnly ? "?unread_only=true" : ""}`,
    ),
  notificationsUnread: () => http<{ count: number }>("/notifications/unread-count"),
  markNotificationRead: (id: string) =>
    http<{ ok: boolean }>(`/notifications/${id}/read`, { method: "POST", body: "{}" }),
  markAllNotificationsRead: () =>
    http<{ ok: boolean }>("/notifications/read-all", { method: "POST", body: "{}" }),
  notificationRules: () => http<{ rules: NotificationRule[] }>("/notifications/rules"),
  upsertNotificationRule: (body: Partial<NotificationRule>) =>
    http<{ rule: NotificationRule }>("/notifications/rules", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteNotificationRule: (id: string) =>
    http<{ ok: boolean }>(`/notifications/rules/${id}`, { method: "DELETE" }),

  // --- Azure Workloads (hand-picked resource scopes) ---
  workloads: () => http<{ workloads: Workload[] }>("/workloads"),
  upsertWorkload: (body: Partial<Workload>) =>
    http<{ workload: Workload }>("/workloads", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteWorkload: (id: string) =>
    http<{ ok: boolean }>(`/workloads/${id}`, { method: "DELETE" }),
  // --- Workload trash (soft-delete) ---
  trashedWorkloads: () => http<{ workloads: Workload[] }>("/workloads/trash"),
  restoreWorkload: (id: string) =>
    http<{ workload: Workload }>(`/workloads/${id}/restore`, { method: "POST", body: "{}" }),
  purgeWorkload: (id: string) =>
    http<{ ok: boolean }>(`/workloads/${id}/purge`, { method: "DELETE" }),
  emptyWorkloadTrash: () =>
    http<{ ok: boolean; deleted: number }>("/workloads/trash/empty", { method: "POST", body: "{}" }),
  mergeWorkloads: (body: { workload_ids: string[]; name?: string }) =>
    http<{ workload: Workload }>("/workloads/merge", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // --- Workload Groups (applications / service families) ---
  workloadGroups: () =>
    http<{ groups: WorkloadGroup[]; ungrouped: number; total_workloads: number }>("/workloads/groups"),
  workloadGroup: (id: string) =>
    http<WorkloadGroupDetail>(`/workloads/groups/${encodeURIComponent(id)}`),
  workloadGroupCompare: (id: string) =>
    http<WorkloadGroupCompareResult>(`/workloads/groups/${encodeURIComponent(id)}/compare`),
  upsertWorkloadGroup: (body: { id?: string; name: string; description?: string; color?: string; owner?: string; tags?: string[] }) =>
    http<{ group: WorkloadGroupBase }>("/workloads/groups", { method: "PUT", body: JSON.stringify(body) }),
  deleteWorkloadGroup: (id: string) =>
    http<{ ok: boolean }>(`/workloads/groups/${encodeURIComponent(id)}`, { method: "DELETE" }),
  assignWorkloadGroup: (body: { group_id?: string; name?: string; workload_ids: string[]; mode?: "add" | "remove" }) =>
    http<{ ok: boolean; updated: number; group: WorkloadGroupBase | null }>("/workloads/groups/assign", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  suggestWorkloadGroups: () =>
    http<{ suggestions: WorkloadGroupSuggestion[] }>("/workloads/groups/suggest", { method: "POST", body: "{}" }),
  workloadOverlaps: (connectionId = "", deep = false) => {
    const q = new URLSearchParams();
    if (connectionId) q.set("connection_id", connectionId);
    if (deep) q.set("deep", "true");
    return http<WorkloadOverlaps>(`/workloads/overlaps?${q.toString()}`);
  },
  workloadTree: (body: {
    connection_id: string;
    group_by?: string;
    kind?: string;
    node_id?: string;
    refresh?: boolean;
  }) =>
    http<{ nodes: TreeNode[] } & CacheMeta>("/workloads/tree", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  workloadSearch: (body: {
    connection_id: string;
    query?: string;
    subscription_id?: string;
    types?: string[];
    locations?: string[];
    skip?: number;
    top?: number;
  }) =>
    http<{ rows: TreeNode[]; error: string }>("/workloads/search", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  workloadFacets: (body: { connection_id: string; subscription_id?: string; refresh?: boolean }) =>
    http<{ types: string[]; locations: string[] } & CacheMeta>("/workloads/facets", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  invalidateWorkloadCache: (connectionId: string) =>
    http<{ ok: boolean; removed: number }>("/workloads/cache/invalidate", {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId }),
    }),
  saveAutopilotWorkloads: (body: {
    connection_id: string;
    scope_kind: string;
    scope_id: string;
    scope_name: string;
    candidates: WorkloadCandidate[];
    decisions?: { action: string; name?: string; from?: string; to?: string; excluded?: string }[];
    auto_assess?: boolean;
    auto_architecture?: boolean;
  }) =>
    http<{ saved: Workload[]; count: number; launched: { missions: string[]; architectures: string[] } }>("/workloads/autopilot/save", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Live re-estimate cost + filter preview for a sculpt config (against the cached survey).
  autopilotEstimate: (body: { connection_id: string; scope_kind: string; scope_id: string; config: SculptConfig }) =>
    http<AutopilotEstimateResult | { needs_survey: true }>("/workloads/autopilot/estimate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Saved discovery profiles.
  autopilotProfiles: (connectionId: string) =>
    http<{ profiles: DiscoveryProfile[] }>(`/workloads/autopilot/profiles?connection_id=${encodeURIComponent(connectionId)}`),
  saveAutopilotProfile: (body: { connection_id: string; name: string; config: SculptConfig; scope_kind?: string; scope_id?: string; scope_name?: string; profile_id?: string }) =>
    http<{ profile: DiscoveryProfile }>("/workloads/autopilot/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteAutopilotProfile: (profileId: string, connectionId: string) =>
    http<{ ok: boolean }>(`/workloads/autopilot/profiles/${encodeURIComponent(profileId)}?connection_id=${encodeURIComponent(connectionId)}`, {
      method: "DELETE",
    }),
  estateCoverage: (connectionId: string) =>
    http<EstateCoverage>(`/workloads/estate-coverage?connection_id=${encodeURIComponent(connectionId)}`),
  refreshWorkload: (id: string) =>
    http<{ workload: Workload; diff: WorkloadRefreshDiff }>(`/workloads/${id}/refresh`, {
      method: "POST",
      body: "{}",
    }),
  // Cache-only command-center profiles (instant; never scans Azure).
  workloadProfile: (id: string) =>
    http<{ profile: WorkloadProfile }>(`/workloads/${encodeURIComponent(id)}/profile`),
  workloadProfiles: (ids: string[] = []) =>
    http<{ profiles: WorkloadProfile[]; total: number }>("/workloads/profiles", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  workloadHealthWeights: () => http<WorkloadHealthWeights>("/workloads/health-weights"),
  recordWorkloadTrend: (id: string) =>
    http<{ recorded: boolean; score: number | null }>(`/workloads/${encodeURIComponent(id)}/trend/record`, { method: "POST", body: "{}" }),
  workloadTrend: (id: string) =>
    http<CoverageTrend>(`/workloads/${encodeURIComponent(id)}/trend`),

  // --- Architectures ---
  architectureCatalog: () => http<ArchitectureCatalog>("/architectures/catalog"),
  architectures: () => http<{ architectures: Architecture[] }>("/architectures"),
  architecture: (id: string) => http<{ architecture: Architecture }>(`/architectures/${id}`),
  upsertArchitecture: (body: Partial<Architecture>) =>
    http<{ architecture: Architecture }>("/architectures", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteArchitecture: (id: string) =>
    http<{ ok: boolean }>(`/architectures/${id}`, { method: "DELETE" }),
  trashedArchitectures: () => http<{ architectures: Architecture[] }>("/architectures/trash"),
  restoreArchitecture: (id: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/restore`, { method: "POST", body: "{}" }),
  purgeArchitecture: (id: string) =>
    http<{ ok: boolean }>(`/architectures/${id}/purge`, { method: "DELETE" }),
  emptyArchitectureTrash: () =>
    http<{ ok: boolean; deleted: number }>("/architectures/trash/empty", { method: "POST", body: "{}" }),
  cloneArchitecture: (id: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/clone`, { method: "POST", body: "{}" }),
  architectureRevisions: (id: string) =>
    http<{ revisions: ArchitectureRevision[] }>(`/architectures/${id}/revisions`),
  architectureRevision: (id: string, revisionId: string) =>
    http<{ revision: ArchitectureRevisionContent }>(`/architectures/${id}/revisions/${revisionId}`),
  architectureActivity: (id: string) =>
    http<{ activity: ArchitectureActivity[] }>(`/architectures/${id}/activity`),
  restoreArchitectureRevision: (id: string, revisionId: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/revisions/${revisionId}/restore`, {
      method: "POST",
      body: "{}",
    }),
  setArchitectureState: (id: string, state: ArchitectureState) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/state`, {
      method: "POST",
      body: JSON.stringify({ state }),
    }),
  setArchitectureCategory: (id: string, categoryId: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/category`, {
      method: "POST",
      body: JSON.stringify({ category_id: categoryId }),
    }),
  setArchitectureWorkload: (id: string, workloadId: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/workload`, {
      method: "POST",
      body: JSON.stringify({ workload_id: workloadId }),
    }),
  rebuildArchitecture: (id: string, workloadId?: string | null, connectionId?: string | null) =>
    http<{ job: ArchitectureJob }>(`/architectures/${id}/rebuild`, {
      method: "POST",
      body: JSON.stringify({ workload_id: workloadId ?? null, connection_id: connectionId ?? null }),
    }),
  // --- Architecture Memory ---
  memoryCatalog: () =>
    http<{ sections: MemorySectionMeta[]; default_keys: string[] }>("/architectures/memory/catalog"),
  architectureMemories: () =>
    http<{ memories: MemoryIndexEntry[] }>("/architectures/memories"),
  architectureMemory: (id: string) =>
    http<ArchitectureMemoryResponse>(`/architectures/${id}/memory`),
  upsertArchitectureMemory: (id: string, body: { title?: string; sections?: MemorySection[]; enabled_for_investigations?: boolean }) =>
    http<ArchitectureMemoryResponse>(`/architectures/${id}/memory`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteArchitectureMemory: (id: string) =>
    http<{ ok: boolean }>(`/architectures/${id}/memory`, { method: "DELETE" }),
  regenerateMemorySection: (id: string, sectionKey: string, extraContext = "") =>
    http<ArchitectureMemoryResponse>(`/architectures/${id}/memory/sections/${encodeURIComponent(sectionKey)}/generate`, {
      method: "POST",
      body: JSON.stringify({ extra_context: extraContext }),
    }),
  memoryRevisions: (id: string) =>
    http<{ revisions: MemoryRevision[] }>(`/architectures/${id}/memory/revisions`),
  memoryRevision: (id: string, revisionId: string) =>
    http<{ revision: MemoryRevisionContent; markdown: string }>(`/architectures/${id}/memory/revisions/${revisionId}`),
  restoreMemoryRevision: (id: string, revisionId: string) =>
    http<ArchitectureMemoryResponse>(`/architectures/${id}/memory/revisions/${revisionId}/restore`, {
      method: "POST",
      body: "{}",
    }),
  // --- Workload Know-Me (multiple per workload; keyed by km_id; draft/published + Trash) ---
  knowMeIndex: () =>
    http<KnowMeIndex>("/architectures/know-me"),
  knowMeTrash: () =>
    http<{ items: KnowMeTrashEntry[] }>("/architectures/know-me/trash"),
  emptyKnowMeTrash: () =>
    http<{ purged: number }>("/architectures/know-me/trash/empty", { method: "POST", body: "{}" }),
  purgeKnowMeOrphan: (architectureId: string) =>
    http<{ ok: boolean; purged_documents: number }>(`/architectures/know-me/orphans/${architectureId}`, { method: "DELETE" }),
  createKnowMe: (architectureId: string, title = "") =>
    http<KnowMeResponse>(`/architectures/${architectureId}/know-me`, {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  knowMe: (kmId: string) =>
    http<KnowMeResponse>(`/architectures/know-me/${kmId}`),
  // KP5/KU4 — current background generation-job status for a Know-Me (for reconnect on mount).
  knowMeGenerateJob: (kmId: string) =>
    http<{ job: { id: string; status: string; last_message: string } | null }>(`/architectures/know-me/${kmId}/generate/job`),
  // In-flight / recently-finished "Build from workload" jobs for this tenant. The build runs
  // detached server-side, so the index can show a background tray and reattach after nav.
  knowMeFromWorkloadActive: () =>
    http<{ jobs: KnowMeBuildJob[] }>("/architectures/know-me/from-workload/active"),
  saveKnowMe: (kmId: string, body: { title?: string; description?: string; sections?: KnowMeSection[]; todos?: KnowMeTodo[]; status?: string }) =>
    http<KnowMeResponse>(`/architectures/know-me/${kmId}`, { method: "PUT", body: JSON.stringify(body) }),
  setKnowMeReference: (kmId: string, isReference = true) =>
    http<KnowMeResponse>(`/architectures/know-me/${kmId}/reference`, { method: "POST", body: JSON.stringify({ is_reference: isReference }) }),
  suggestKnowMeField: (kmId: string, fieldId: string) =>
    http<{ choices: string[]; choice_source?: string }>(`/architectures/know-me/${kmId}/fields/${encodeURIComponent(fieldId)}/suggest`, { method: "POST", body: "{}" }),
  deleteKnowMe: (kmId: string) =>
    http<{ ok: boolean }>(`/architectures/know-me/${kmId}`, { method: "DELETE" }),
  restoreKnowMe: (kmId: string) =>
    http<{ ok: boolean; know_me: KnowMe }>(`/architectures/know-me/${kmId}/restore`, { method: "POST", body: "{}" }),
  purgeKnowMe: (kmId: string) =>
    http<{ ok: boolean }>(`/architectures/know-me/${kmId}/purge`, { method: "DELETE" }),
  knowMeRevisions: (kmId: string) =>
    http<{ revisions: KnowMeRevision[] }>(`/architectures/know-me/${kmId}/revisions`),
  knowMeRevision: (kmId: string, revisionId: string) =>
    http<{ revision: KnowMeRevisionContent; markdown: string }>(`/architectures/know-me/${kmId}/revisions/${revisionId}`),
  restoreKnowMeRevision: (kmId: string, revisionId: string) =>
    http<KnowMeResponse>(`/architectures/know-me/${kmId}/revisions/${revisionId}/restore`, { method: "POST", body: "{}" }),
  knowMeExportUrl: (kmId: string, format: "md" | "pdf") =>
    `${API_BASE}/architectures/know-me/${kmId}/export?format=${format}`,
  knowMeMermaid: (kmId: string) =>
    http<{ mermaid: string }>(`/architectures/know-me/${kmId}/mermaid`),
  knowMeAssets: (kmId: string) =>
    http<{ assets: KnowMeAsset[] }>(`/architectures/know-me/${kmId}/assets`),
  knowMeAssetUrl: (kmId: string, assetId: string) =>
    `${API_BASE}/architectures/know-me/${kmId}/assets/${assetId}`,
  uploadKnowMeAsset: async (kmId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API_BASE}/architectures/know-me/${kmId}/assets`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const b = await res.json();
        if (b?.detail) detail = b.detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return (await res.json()) as { asset: KnowMeAsset & { ref: string; markdown: string } };
  },
  deleteKnowMeAsset: (kmId: string, assetId: string) =>
    http<{ ok: boolean }>(`/architectures/know-me/${kmId}/assets/${assetId}`, { method: "DELETE" }),
  architectureCollections: () =>
    http<{ collections: ArchitectureCollection[] }>("/architectures/collections"),
  upsertArchitectureCollection: (body: Partial<ArchitectureCollection>) =>
    http<{ collection: ArchitectureCollection }>("/architectures/collections", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteArchitectureCollection: (id: string) =>
    http<{ ok: boolean; reassigned: number }>(`/architectures/collections/${id}`, { method: "DELETE" }),
  reorderArchitectureCollections: (orderedIds: string[]) =>
    http<{ ok: boolean }>("/architectures/collections/reorder", {
      method: "POST",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  enhanceArchitecture: (id: string, goal: string) =>
    http<{ architecture: Architecture }>(`/architectures/${id}/enhance`, {
      method: "POST",
      body: JSON.stringify({ goal }),
    }),
  askArchitecture: (id: string, question: string) =>
    http<{ answer: string }>(`/architectures/${id}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  architectureDrift: (id: string) =>
    http<ArchitectureDrift>(`/architectures/${id}/drift`, { method: "POST", body: "{}" }),
  workloadInventory: (workloadId: string) =>
    http<{ count: number; error: string; resources: unknown[] }>(
      `/architectures/workload/${workloadId}/inventory`,
    ),
  architectureJobs: () => http<{ jobs: ArchitectureJob[] }>("/architectures/jobs"),
  createArchitectureJobs: (workloadIds: string[], connectionId?: string | null) =>
    http<{ jobs: ArchitectureJob[]; queued: number }>("/architectures/jobs", {
      method: "POST",
      body: JSON.stringify({ workload_ids: workloadIds, connection_id: connectionId ?? null }),
    }),
  cancelArchitectureJob: (id: string) =>
    http<{ ok: boolean }>(`/architectures/jobs/${id}/cancel`, { method: "POST", body: "{}" }),
  dismissArchitectureJob: (id: string) =>
    http<{ ok: boolean }>(`/architectures/jobs/${id}`, { method: "DELETE" }),

  // --- Assessments ---
  assessmentCatalog: () => http<AssessmentCatalog>("/assessments/catalog"),
  assessmentRuns: (workloadId?: string) =>
    http<{ runs: AssessmentRunSummary[] }>(
      `/assessments/runs${workloadId ? `?workload_id=${encodeURIComponent(workloadId)}` : ""}`,
    ),
  assessmentRun: (id: string) => http<{ run: AssessmentRunDetail }>(`/assessments/runs/${id}`),
  enqueueAssessments: (body: {
    workload_ids: string[];
    pillars?: string[];
    pack?: string | null;
    connection_id?: string | null;
    use_ai?: boolean;
  }) =>
    http<{ runs: AssessmentRunSummary[]; queued: number }>("/assessments/enqueue", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  assessmentActionPlan: (id: string) =>
    http<AssessmentActionPlan>(`/assessments/runs/${id}/action-plan`),
  assessmentByResource: (id: string) =>
    http<{ run_id: string; workload_name: string | null; resources: AssessmentResourceRollup[]; count: number }>(
      `/assessments/runs/${id}/by-resource`,
    ),
  assessmentAttestations: (workloadId: string) =>
    http<{ attestations: Record<string, { status: string; note?: string; by?: string; at?: string }> }>(
      `/assessments/attestations?workload_id=${encodeURIComponent(workloadId)}`,
    ),
  setAssessmentAttestation: (body: { workload_id: string; check_id: string; status: string; note?: string }) =>
    http<{ attestation: unknown }>("/assessments/attestations", { method: "PUT", body: JSON.stringify(body) }),
  deleteAssessmentRun: (id: string) =>
    http<{ ok: boolean }>(`/assessments/runs/${id}`, { method: "DELETE" }),
  cancelAssessmentRun: (id: string) =>
    http<{ run: AssessmentRunSummary }>(`/assessments/runs/${id}/cancel`, { method: "POST", body: "{}" }),
  assessmentTrash: () => http<{ runs: AssessmentRunSummary[] }>("/assessments/trash"),
  restoreAssessmentRun: (id: string) =>
    http<{ run: AssessmentRunSummary }>(`/assessments/runs/${id}/restore`, { method: "POST", body: "{}" }),
  purgeAssessmentRun: (id: string) =>
    http<{ ok: boolean }>(`/assessments/runs/${id}/purge`, { method: "DELETE" }),
  emptyAssessmentTrash: () =>
    http<{ ok: boolean; purged: number }>("/assessments/trash/empty", { method: "POST", body: "{}" }),
  setAssessmentBaseline: (id: string, baseline: boolean) =>
    http<{ run: AssessmentRunSummary }>(`/assessments/runs/${id}/baseline`, {
      method: "POST",
      body: JSON.stringify({ baseline }),
    }),
  assessmentTrend: (workloadId: string) =>
    http<{ workload_id: string; points: AssessmentTrendPoint[] }>(
      `/assessments/trend?workload_id=${encodeURIComponent(workloadId)}`,
    ),
  assessmentPortfolio: () => http<{ workloads: AssessmentPortfolioRow[] }>("/assessments/portfolio"),
  // Waivers
  assessmentWaivers: (workloadId?: string) =>
    http<{ waivers: AssessmentWaiver[] }>(
      `/assessments/waivers${workloadId ? `?workload_id=${encodeURIComponent(workloadId)}` : ""}`,
    ),
  createAssessmentWaiver: (body: {
    workload_id: string;
    check_id: string;
    resource_id?: string | null;
    justification: string;
    approver: string;
    expires_at?: string | null;
  }) => http<{ waiver: AssessmentWaiver }>("/assessments/waivers", { method: "POST", body: JSON.stringify(body) }),
  revokeAssessmentWaiver: (id: string) =>
    http<{ ok: boolean }>(`/assessments/waivers/${id}`, { method: "DELETE" }),
  // Finding ownership / state
  assessmentFindingStates: (workloadId: string) =>
    http<{ states: Record<string, AssessmentFindingStateT> }>(
      `/assessments/finding-states?workload_id=${encodeURIComponent(workloadId)}`,
    ),
  updateAssessmentFindingState: (body: {
    workload_id: string;
    check_id: string;
    status?: string;
    assignee?: string;
    due_date?: string | null;
    notes?: string;
  }) => http<{ state: AssessmentFindingStateT }>("/assessments/finding-states", { method: "PUT", body: JSON.stringify(body) }),
  // Remediation ticket
  createAssessmentTicket: (body: { run_id: string; check_id: string; connector_id: string }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/assessments/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  // Identity dashboard
  identityOverview: (days: number, connectionId?: string | null) => {
    const q = new URLSearchParams({ days: String(days) });
    if (connectionId) q.set("connection_id", connectionId);
    return http<IdentityOverview>(`/identity/overview?${q.toString()}`);
  },
  refreshIdentity: (days: number, connectionId?: string | null) => {
    const q = new URLSearchParams({ days: String(days) });
    if (connectionId) q.set("connection_id", connectionId);
    return http<IdentityOverview>(`/identity/refresh?${q.toString()}`, { method: "POST" });
  },
  createIdentityTicket: (body: { connector_id: string; finding: IdentityFinding }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/identity/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  // PIM / JIT lifecycle review — server-cached; visiting reads, refresh recomputes.
  pimOverview: (connectionId?: string | null) => {
    const q = new URLSearchParams();
    if (connectionId) q.set("connection_id", connectionId);
    const qs = q.toString();
    return http<PimOverview>(`/identity/pim${qs ? `?${qs}` : ""}`);
  },
  refreshPim: (connectionId?: string | null) => {
    const q = new URLSearchParams();
    if (connectionId) q.set("connection_id", connectionId);
    const qs = q.toString();
    return http<PimOverview>(`/identity/pim/refresh${qs ? `?${qs}` : ""}`, { method: "POST" });
  },
  // App Registrations (Identity tab) — server-cached; refresh forces a re-pull.
  appRegistrations: (connectionId?: string | null) =>
    http<AppRegistrationsResponse>(
      "/identity/app-registrations" +
        (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),
  refreshAppRegistrations: (connectionId?: string | null) =>
    http<AppRegistrationsResponse>(
      "/identity/app-registrations/refresh" +
        (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
      { method: "POST", body: "{}" },
    ),
  // Multi-sheet Excel workbook (Applications / Credentials / Permissions / Owners / High
  // Risk / Permission Pivot) of the full cached snapshot — direct download (cookie auth).
  appRegistrationsWorkbookUrl: (connectionId?: string | null) =>
    `${API_BASE}/identity/app-registrations/workbook` +
    (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
  appRegistrationsJob: (connectionId?: string | null) =>
    http<{ job: { id: string; status: string; started_at: string; finished_at: string | null; progress_count: number; last_message: string; error: string } | null }>(
      "/identity/app-registrations/job" +
        (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),
  // Reservations Monitor — server cache; demo scope for synthetic data.
  reservationsOverview: (demo = false, connectionId = "") =>
    http<ReservationsSnapshot>(`/reservations/overview?demo=${demo ? "true" : "false"}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  refreshReservations: (demo = false, connectionId = "") =>
    http<ReservationsSnapshot>(`/reservations/refresh?demo=${demo ? "true" : "false"}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`, {
      method: "POST",
      body: "{}",
    }),
  reservationsDigestPreview: (demo = false, connectionId = "") =>
    http<ReservationsDigestPreview>(`/reservations/digest/preview?demo=${demo ? "true" : "false"}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  // Quota Monitor — server cache; demo scope for synthetic data; manual scan only.
  quotaMeta: () => http<QuotaMeta>("/quota/meta"),
  quotaSubscriptions: (connectionId = "") =>
    http<{ subscriptions: { id: string; name: string; state?: string; is_default?: boolean }[]; error: string }>(
      `/quota/subscriptions${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`,
    ),
  quotaRegions: (subscriptionId: string, connectionId = "") =>
    http<{ regions: QuotaRegion[]; error: string }>(
      `/quota/regions?subscription_id=${encodeURIComponent(subscriptionId)}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`,
    ),
  quotaProviders: (subscriptionId: string, connectionId = "") =>
    http<{ providers: QuotaProviderReg[]; error: string }>(
      `/quota/providers?subscription_id=${encodeURIComponent(subscriptionId)}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`,
    ),
  quotaOverview: (subscriptionId = "", connectionId = "", demo = false) =>
    http<QuotaSnapshot>(
      `/quota/overview?subscription_id=${encodeURIComponent(subscriptionId)}&demo=${demo ? "true" : "false"}${connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""}`,
    ),
  runQuotaScan: (p: QuotaScanParams) => {
    const params = new URLSearchParams();
    params.set("subscription_id", p.subscription_id);
    params.set("demo", p.demo ? "true" : "false");
    if (p.connection_id) params.set("connection_id", p.connection_id);
    if (p.regions && p.regions.length) params.set("regions", p.regions.join(","));
    if (p.categories && p.categories.length) params.set("categories", p.categories.join(","));
    if (p.include_unused) params.set("include_unused", "true");
    return http<QuotaSnapshot>(`/quota/scan?${params.toString()}`, { method: "POST", body: "{}" });
  },
  quotaRuns: (subscriptionId = "", limit = 30) =>
    http<{ runs: QuotaRun[] }>(
      `/quota/runs?subscription_id=${encodeURIComponent(subscriptionId)}&limit=${limit}`,
    ),
  quotaExportUrl: (subscriptionId = "", demo = false, format: "csv" | "json" = "csv") =>
    `${API_BASE}/quota/export?subscription_id=${encodeURIComponent(subscriptionId)}&demo=${demo ? "true" : "false"}&format=${format}`,
  // Ownership — owners/teams directory, assignments, people-picker, resolver.
  ownershipOwners: () => http<{ owners: Owner[]; total: number }>("/ownership/owners"),
  ownershipOwner: (id: string) => http<Owner & { assignments: OwnershipAssignment[] }>(`/ownership/owners/${encodeURIComponent(id)}`),
  upsertOwner: (body: Partial<Owner>) =>
    http<Owner>("/ownership/owners", { method: "POST", body: JSON.stringify(body) }),
  ownerFromDirectory: (hit: DirectoryHit) =>
    http<Owner>("/ownership/owners/from-directory", {
      method: "POST",
      body: JSON.stringify({
        source: hit.source, kind: hit.kind, display_name: hit.display_name,
        email: hit.email, link: hit.link, group_ref: hit.group_ref || {},
      }),
    }),
  deleteOwner: (id: string) => http<{ ok: boolean }>(`/ownership/owners/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restoreOwner: (id: string) => http<Owner>(`/ownership/owners/${encodeURIComponent(id)}/restore`, { method: "POST", body: "{}" }),
  purgeOwner: (id: string) => http<{ ok: boolean }>(`/ownership/owners/${encodeURIComponent(id)}/purge`, { method: "DELETE" }),
  ownersTrash: () => http<{ owners: Owner[] }>("/ownership/owners/trash"),
  emptyOwnersTrash: () => http<{ purged: number }>("/ownership/owners/trash/empty", { method: "POST", body: "{}" }),
  ownershipAssignments: (params: { subject_kind?: string; subject_id?: string; owner_id?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.subject_kind) qs.set("subject_kind", params.subject_kind);
    if (params.subject_id) qs.set("subject_id", params.subject_id);
    if (params.owner_id) qs.set("owner_id", params.owner_id);
    const q = qs.toString();
    return http<{ assignments: OwnershipAssignment[]; total: number }>(`/ownership/assignments${q ? `?${q}` : ""}`);
  },
  upsertAssignment: (body: Partial<OwnershipAssignment>) =>
    http<OwnershipAssignment>("/ownership/assignments", { method: "POST", body: JSON.stringify(body) }),
  bulkAssign: (owner_id: string, role: string, primary: boolean, subjects: Partial<OwnershipAssignment>[]) =>
    http<{ created: OwnershipAssignment[]; count: number }>("/ownership/assignments/bulk", {
      method: "POST",
      body: JSON.stringify({ owner_id, role, primary, subjects }),
    }),
  transferOwnership: (from_owner_id: string, to_owner_id: string) =>
    http<{ moved: number }>("/ownership/assignments/transfer", {
      method: "POST",
      body: JSON.stringify({ from_owner_id, to_owner_id }),
    }),
  deleteAssignment: (id: string) => http<{ ok: boolean }>(`/ownership/assignments/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restoreAssignment: (id: string) => http<OwnershipAssignment>(`/ownership/assignments/${encodeURIComponent(id)}/restore`, { method: "POST", body: "{}" }),
  assignmentsTrash: () => http<{ assignments: OwnershipAssignment[] }>("/ownership/assignments/trash"),
  emptyAssignmentsTrash: () => http<{ purged: number }>("/ownership/assignments/trash/empty", { method: "POST", body: "{}" }),
  directorySearch: (q: string, connectionId = "", includeEntra = true) => {
    const qs = new URLSearchParams({ q, include_entra: includeEntra ? "true" : "false" });
    if (connectionId) qs.set("connection_id", connectionId);
    return http<DirectorySearch>(`/ownership/directory/search?${qs.toString()}`);
  },
  resolveOwner: (subject_kind: string, subject_id: string, subscription_id = "", resource_group = "") => {
    const qs = new URLSearchParams({ subject_kind, subject_id });
    if (subscription_id) qs.set("subscription_id", subscription_id);
    if (resource_group) qs.set("resource_group", resource_group);
    return http<ResolveResult>(`/ownership/resolve?${qs.toString()}`);
  },
  resolveOwnerBatch: (subjects: { subject_kind: string; subject_id: string; tags?: any; subscription_id?: string; resource_group?: string }[]) =>
    http<{ results: ResolveResult[] }>("/ownership/resolve/batch", { method: "POST", body: JSON.stringify({ subjects }) }),
  ownershipSubjects: (scope?: OwnershipScope, connectionId = "") => http<{ subjects: OwnershipSubject[]; total: number; owned: number; unowned: number }>(`/ownership/subjects${ownScopeQs(scope, connectionId)}`),
  ownershipCoverage: (scopeKind: string, workloadId: string, scopeId: string, connectionId = "") => {
    const qs = new URLSearchParams({ scope_kind: scopeKind });
    if (workloadId) qs.set("workload_id", workloadId);
    if (scopeId) qs.set("scope_id", scopeId);
    if (connectionId) qs.set("connection_id", connectionId);
    return http<OwnershipCoverage>(`/ownership/coverage?${qs.toString()}`);
  },
  refreshOwnershipCoverage: (scopeKind: string, workloadId: string, scopeId: string, connectionId = "") => {
    const qs = new URLSearchParams({ scope_kind: scopeKind });
    if (workloadId) qs.set("workload_id", workloadId);
    if (scopeId) qs.set("scope_id", scopeId);
    if (connectionId) qs.set("connection_id", connectionId);
    return http<OwnershipCoverage>(`/ownership/refresh?${qs.toString()}`, { method: "POST", body: "{}" });
  },
  ownershipTrend: (scopeKind: string, workloadId: string, scopeId: string, connectionId = "") => {
    const qs = new URLSearchParams({ scope_kind: scopeKind });
    if (workloadId) qs.set("workload_id", workloadId);
    if (scopeId) qs.set("scope_id", scopeId);
    if (connectionId) qs.set("connection_id", connectionId);
    return http<CoverageTrend>(`/ownership/trend?${qs.toString()}`);
  },
  ownershipEstate: (ownerId = "") =>
    http<OwnershipEstateResponse>(`/ownership/estate${ownerId ? `?owner_id=${encodeURIComponent(ownerId)}` : ""}`),
  ownershipSuggestions: (scope?: OwnershipScope, connectionId = "") => http<{ suggestions: OwnershipSuggestion[]; total: number; note: string }>(`/ownership/suggestions${ownScopeQs(scope, connectionId)}`),
  acceptSuggestion: (s: OwnershipSuggestion) =>
    http<{ owner: Owner; assignment: OwnershipAssignment }>("/ownership/suggestions/accept", {
      method: "POST",
      body: JSON.stringify({
        subject_kind: s.subject_kind, subject_id: s.subject_id, subject_name: s.subject_name,
        candidate: s.candidate, role: s.role, primary: true,
      }),
    }),
  ownershipAttestation: (scope?: OwnershipScope) => http<{ items: AttestationItem[]; summary: { total: number; never: number; stale: number; fresh: number; stale_days: number } }>(`/ownership/attestation${ownScopeQs(scope)}`),
  attestAssignment: (id: string) => http<OwnershipAssignment>(`/ownership/assignments/${encodeURIComponent(id)}/attest`, { method: "POST", body: "{}" }),
  ownershipLeavers: () => http<{ at_risk: LeaverRisk[]; count: number }>("/ownership/leavers"),
  ownershipWritebackStatus: () => http<{ enabled: boolean }>("/ownership/writeback/status"),
  ownershipWritebackIac: (resourceId: string, owner = "", ownerEmail = "") =>
    http<{ bicep: string; policy: string }>("/ownership/writeback/iac", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId, owner, owner_email: ownerEmail }),
    }),
  ownershipWritebackApply: (resourceId: string, owner = "", ownerEmail = "", connectionId = "") =>
    http<{ ok: boolean; error: string; applied: Record<string, string> }>("/ownership/writeback/apply", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId, owner, owner_email: ownerEmail, connection_id: connectionId || null }),
    }),
  // Owners export / AI import / owner→tag apply / revisions.
  ownersExportUrl: (format: "csv" | "xlsx") => `${apiBase}/ownership/owners/export?format=${format}`,
  ownersExport: (format: "csv" | "xlsx") => httpBlob(`/ownership/owners/export?format=${format}`),
  ownersTemplate: () => httpBlob("/ownership/owners/template"),
  ownersImportPreview: (file: File, sheetName?: string) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (sheetName) form.append("sheet_name", sheetName);
    return httpUpload<OwnerImportPreview>("/ownership/owners/import/preview", form);
  },
  ownersImportConfirm: (rows: Record<string, unknown>[], mapping: Record<string, string>, createAssignments: boolean) =>
    http<OwnerImportResult>("/ownership/owners/import", {
      method: "POST",
      body: JSON.stringify({ rows, mapping, create_assignments: createAssignments }),
    }),
  ownerTagApplyPreview: (body: OwnerTagApplyReq) =>
    http<OwnerTagPlan>("/ownership/tag-apply/preview", { method: "POST", body: JSON.stringify(body) }),
  ownerTagApply: (body: OwnerTagApplyReq & { approved: boolean }) =>
    http<OwnerTagApplyResult>("/ownership/tag-apply", { method: "POST", body: JSON.stringify(body) }),
  ownerTagRevisions: (connectionId = "") =>
    http<{ revisions: TagRevision[] }>(`/ownership/tag-revisions${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  ownerTagRevision: (id: string) =>
    http<{ revision: TagRevision; diff: TagRevisionDiffRow[] }>(`/ownership/tag-revisions/${encodeURIComponent(id)}`),
  revertOwnerTagRevision: (id: string, connectionId = "") =>
    http<TagRevertResult>(`/ownership/tag-revisions/${encodeURIComponent(id)}/revert`, {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId, approved: true }),
    }),
  // Tag-intelligence revisions (all tag changes — tagintel + ownership).
  tagRevisions: (connectionId = "", source = "") => {
    const qs = new URLSearchParams();
    if (connectionId) qs.set("connection_id", connectionId);
    if (source) qs.set("source", source);
    const q = qs.toString();
    return http<{ revisions: TagRevision[] }>(`/tagintel/revisions${q ? `?${q}` : ""}`);
  },
  tagRevision: (id: string) =>
    http<{ revision: TagRevision; diff: TagRevisionDiffRow[] }>(`/tagintel/revisions/${encodeURIComponent(id)}`),
  revertTagRevision: (id: string, connectionId = "") =>
    http<TagRevertResult>(`/tagintel/revisions/${encodeURIComponent(id)}/revert`, {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId, approved: true }),
    }),
  // RBAC / Access Review — server cache, per-scope refresh.
  rbacOverview: (connectionId?: string | null) =>
    http<RbacOverview>(`/rbac/overview${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  rbacAccess: (params: {
    tab?: string;
    scope?: string;
    surface?: string;
    principal_type?: string;
    search?: string;
    privileged_only?: boolean;
    scope_id?: string;
    subscription_ids?: string;
    workload_id?: string;
    offset?: number;
    limit?: number;
    connection_id?: string | null;
  }) => {
    const q = new URLSearchParams();
    if (params.tab) q.set("tab", params.tab);
    if (params.scope) q.set("scope", params.scope);
    if (params.surface) q.set("surface", params.surface);
    if (params.principal_type) q.set("principal_type", params.principal_type);
    if (params.search) q.set("search", params.search);
    if (params.privileged_only) q.set("privileged_only", "true");
    if (params.scope_id) q.set("scope_id", params.scope_id);
    if (params.subscription_ids) q.set("subscription_ids", params.subscription_ids);
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.offset != null) q.set("offset", String(params.offset));
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<RbacAccessPage>(`/rbac/access?${q.toString()}`);
  },
  rbacScopeTree: (connectionId?: string | null) => http<RbacScopeTree>(`/rbac/scope-tree${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  rbacScopes: (connectionId?: string | null) => http<{ scopes: RbacScopeFreshness[]; directory: RbacDirectoryFreshness; ttl_s: number }>(`/rbac/scopes${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  rbacRoles: (connectionId?: string | null) => http<{ role_defs: Record<string, unknown>[]; principals: Record<string, unknown>[] }>(`/rbac/roles${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  rbacPivots: (params?: { scope_id?: string; subscription_ids?: string; workload_id?: string; connection_id?: string | null }) => {
    const q = new URLSearchParams();
    if (params?.scope_id) q.set("scope_id", params.scope_id);
    if (params?.subscription_ids) q.set("subscription_ids", params.subscription_ids);
    if (params?.workload_id) q.set("workload_id", params.workload_id);
    if (params?.connection_id) q.set("connection_id", params.connection_id);
    const qs = q.toString();
    return http<RbacPivots>(`/rbac/pivots${qs ? `?${qs}` : ""}`);
  },
  rbacDiagnostics: (connectionId?: string | null) =>
    http<{ collectors: RbacCollector[]; errors: Record<string, string>[]; directory: RbacDirectoryFreshness }>(`/rbac/diagnostics${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`),
  rbacRefresh: (body: { scope?: string; mode?: string; display_name?: string; connection_id?: string | null }) =>
    http<RbacJob & { already_running: boolean }>("/rbac/refresh", { method: "POST", body: JSON.stringify(body) }),
  rbacJob: (params: { scope?: string; mode?: string; connection_id?: string | null }) => {
    const q = new URLSearchParams();
    if (params.scope) q.set("scope", params.scope);
    if (params.mode) q.set("mode", params.mode);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<{ job: RbacJob }>(`/rbac/job?${q.toString()}`);
  },
  rbacRuns: () => http<{ runs: RbacRun[] }>("/rbac/runs"),
  rbacRun: (id: string) => http<{ run: RbacRun | null }>(`/rbac/run/${encodeURIComponent(id)}`),
  rbacExportUrl: (fmt: "csv" | "json", tab: string, filter?: { scope_id?: string; subscription_ids?: string; workload_id?: string; connection_id?: string | null }) => {
    const q = new URLSearchParams({ fmt, tab });
    if (filter?.scope_id) q.set("scope_id", filter.scope_id);
    if (filter?.subscription_ids) q.set("subscription_ids", filter.subscription_ids);
    if (filter?.workload_id) q.set("workload_id", filter.workload_id);
    if (filter?.connection_id) q.set("connection_id", filter.connection_id);
    return `${API_BASE}/rbac/export?${q.toString()}`;
  },
  rbacWorkbookUrl: (filter?: { scope_id?: string; subscription_ids?: string; workload_id?: string; connection_id?: string | null }) => {
    const q = new URLSearchParams();
    if (filter?.scope_id) q.set("scope_id", filter.scope_id);
    if (filter?.subscription_ids) q.set("subscription_ids", filter.subscription_ids);
    if (filter?.workload_id) q.set("workload_id", filter.workload_id);
    if (filter?.connection_id) q.set("connection_id", filter.connection_id);
    const qs = q.toString();
    return `${API_BASE}/rbac/export/workbook${qs ? `?${qs}` : ""}`;
  },
  rbacDemoSeed: () => http<{ ok: boolean; scopes: number; directory_rows: number; overview: RbacOverview }>("/rbac/demo/seed", { method: "POST", body: "{}" }),
  rbacDemoPurge: () => http<{ ok: boolean; scopes_removed: number }>("/rbac/demo/purge", { method: "POST", body: "{}" }),
  // Coverage / posture trend (shared shape across the 4 dashboards).
  coverageTrend: (feature: "amba" | "telemetry" | "backupdr" | "performance", params: { workload_id?: string; subscription_id?: string }) => {
    const base = feature === "performance" ? "/performance/trend" : `/${feature}/trend`;
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<CoverageTrend>(`${base}?${q.toString()}`);
  },
  // Coverage scan history (shared across Monitoring / Telemetry / Backup-DR).
  coverageRuns: (feature: CoverageFeature, params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ runs: CoverageRunSummary[] }>(`/${feature}/runs?${q.toString()}`);
  },
  coverageTrashedRuns: (feature: CoverageFeature, params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ runs: CoverageRunSummary[] }>(`/${feature}/runs/trash?${q.toString()}`);
  },
  coverageRun: <T>(feature: CoverageFeature, runId: string) =>
    http<{ ok: boolean; run?: T; detail?: string }>(`/${feature}/run/${runId}`),
  deleteCoverageRun: (feature: CoverageFeature, runId: string) =>
    http<{ ok: boolean }>(`/${feature}/run/${runId}`, { method: "DELETE" }),
  restoreCoverageRun: (feature: CoverageFeature, runId: string) =>
    http<{ ok: boolean }>(`/${feature}/run/${runId}/restore`, { method: "POST", body: "{}" }),
  purgeCoverageRun: (feature: CoverageFeature, runId: string) =>
    http<{ ok: boolean }>(`/${feature}/run/${runId}/purge`, { method: "DELETE" }),
  emptyCoverageTrash: (feature: CoverageFeature, params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ purged: number }>(`/${feature}/runs/trash/empty?${q.toString()}`, { method: "POST", body: "{}" });
  },
  // AMBA Monitoring Coverage
  // Coverage reports — branded PDF download + Evidence Locker capture (shared by amba/telemetry/backupdr).
  coverageReportPdf: (
    feature: CoverageFeature,
    params: { workload_id?: string; subscription_id?: string },
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return httpBlob(`/${feature}/coverage/pdf?${q.toString()}`, { signal });
  },
  coverageSaveEvidence: (
    feature: CoverageFeature,
    params: { workload_id?: string; subscription_id?: string },
  ) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ ok: boolean; snapshot: { id: string; name: string; sha256: string } }>(
      `/${feature}/coverage/evidence?${q.toString()}`,
      { method: "POST", body: "{}" },
    );
  },
  estateCoveragePdf: (params: { workload_id?: string; subscription_id?: string }, signal?: AbortSignal) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return httpBlob(`/coverage-reports/estate/pdf?${q.toString()}`, { signal });
  },
  ambaCoverage: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<AmbaCoverage>(`/amba/coverage?${q.toString()}`);
  },
  refreshAmba: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<AmbaCoverage>(`/amba/refresh?${q.toString()}`, { method: "POST", body: "{}" });
  },
  ambaIac: (body: { gaps: AmbaGap[]; format: "bicep" | "terraform" }) =>
    http<{ format: string; iac: string; gap_count: number }>("/amba/iac", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  registerAmbaFindings: (body: { workload_id: string; workload_name: string; gaps: AmbaGap[] }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/amba/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createAmbaTicket: (body: { connector_id: string; gap: AmbaGap }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/amba/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  sendAmbaApproval: (body: { scope_kind: string; scope_id: string; scope_name: string; gaps: AmbaGap[]; format: "bicep" | "terraform" }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>("/amba/approval", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  ambaApprovals: (status?: string) =>
    http<{ requests: AmbaChangeRequest[] }>(`/amba/approvals${status ? `?status=${status}` : ""}`),
  ambaApproval: (id: string) => http<{ ok: boolean; request: AmbaChangeRequest }>(`/amba/approvals/${id}`),
  decideAmbaApproval: (id: string, body: { decision: string; reason?: string }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>(`/amba/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteAmbaApproval: (id: string) => http<{ ok: boolean }>(`/amba/approvals/${id}`, { method: "DELETE" }),
  ambaReference: () => http<AmbaReference>("/amba/reference"),
  updateAmbaReference: (body: { types: AmbaReference["types"]; reason?: string }) =>
    http<AmbaReference>("/amba/reference", { method: "PUT", body: JSON.stringify(body) }),
  ambaReferenceRevisions: () => http<{ revisions: AmbaReferenceRevision[] }>("/amba/reference/revisions"),
  restoreAmbaReference: (revision_id: string) =>
    http<{ ok: boolean; reference?: AmbaReference }>("/amba/reference/restore", {
      method: "POST",
      body: JSON.stringify({ revision_id }),
    }),
  resetAmbaReference: () =>
    http<{ ok: boolean; reference: AmbaReference }>("/amba/reference/reset", { method: "POST", body: "{}" }),
  seedAmbaDemo: () =>
    http<{ ok: boolean; workload_id: string; coverage_pct: number }>("/amba/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Telemetry Coverage (diagnostic settings auditor)
  telemetryCoverage: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<TelemetryCoverage>(`/telemetry/coverage?${q.toString()}`);
  },
  refreshTelemetry: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<TelemetryCoverage>(`/telemetry/refresh?${q.toString()}`, { method: "POST", body: "{}" });
  },
  telemetryIac: (body: { gaps: TelemetryGap[]; format: "bicep" | "policy"; workspace_id?: string }) =>
    http<{ format: string; iac: string; gap_count: number }>("/telemetry/iac", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  registerTelemetryFindings: (body: { workload_id: string; workload_name: string; gaps: TelemetryGap[] }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/telemetry/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createTelemetryTicket: (body: { connector_id: string; gap: TelemetryGap }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/telemetry/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  sendTelemetryApproval: (body: { scope_kind: string; scope_id: string; scope_name: string; gaps: TelemetryGap[]; format: "bicep" | "policy"; workspace_id?: string }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>("/telemetry/approval", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  telemetryApprovals: (status?: string) =>
    http<{ requests: AmbaChangeRequest[] }>(`/telemetry/approvals${status ? `?status=${status}` : ""}`),
  decideTelemetryApproval: (id: string, body: { decision: string; reason?: string }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>(`/telemetry/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteTelemetryApproval: (id: string) => http<{ ok: boolean }>(`/telemetry/approvals/${id}`, { method: "DELETE" }),
  telemetryReference: () => http<TelemetryReference>("/telemetry/reference"),
  updateTelemetryReference: (body: { types: TelemetryReference["types"]; reason?: string }) =>
    http<TelemetryReference>("/telemetry/reference", { method: "PUT", body: JSON.stringify(body) }),
  telemetryReferenceRevisions: () => http<{ revisions: TelemetryReferenceRevision[] }>("/telemetry/reference/revisions"),
  restoreTelemetryReference: (revision_id: string) =>
    http<{ ok: boolean; reference?: TelemetryReference }>("/telemetry/reference/restore", {
      method: "POST",
      body: JSON.stringify({ revision_id }),
    }),
  resetTelemetryReference: () =>
    http<{ ok: boolean; reference: TelemetryReference }>("/telemetry/reference/reset", { method: "POST", body: "{}" }),
  telemetryWorkspaces: () => http<{ workspaces: TelemetryWorkspace[]; approved: string[] }>("/telemetry/workspaces"),
  setTelemetryApprovedWorkspaces: (workspaces: string[]) =>
    http<{ ok: boolean; approved: string[] }>("/telemetry/approved-workspaces", {
      method: "PUT",
      body: JSON.stringify({ workspaces }),
    }),
  seedTelemetryDemo: () =>
    http<{ ok: boolean; workload_id: string; coverage_pct: number }>("/telemetry/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Backup & DR Coverage
  backupDrCoverage: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<BackupDrCoverage>(`/backupdr/coverage?${q.toString()}`);
  },
  refreshBackupDr: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<BackupDrCoverage>(`/backupdr/refresh?${q.toString()}`, { method: "POST", body: "{}" });
  },
  backupDrIac: (body: { gaps: BackupDrGap[]; format: "bicep" | "runbook" }) =>
    http<{ format: string; iac: string; gap_count: number }>("/backupdr/iac", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  registerBackupDrFindings: (body: { workload_id: string; workload_name: string; gaps: BackupDrGap[] }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/backupdr/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createBackupDrTicket: (body: { connector_id: string; gap: BackupDrGap }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/backupdr/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  sendBackupDrApproval: (body: { scope_kind: string; scope_id: string; scope_name: string; gaps: BackupDrGap[]; format: "bicep" | "runbook" }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>("/backupdr/approval", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  backupDrApprovals: (status?: string) =>
    http<{ requests: AmbaChangeRequest[] }>(`/backupdr/approvals${status ? `?status=${status}` : ""}`),
  decideBackupDrApproval: (id: string, body: { decision: string; reason?: string }) =>
    http<{ ok: boolean; request: AmbaChangeRequest }>(`/backupdr/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteBackupDrApproval: (id: string) => http<{ ok: boolean }>(`/backupdr/approvals/${id}`, { method: "DELETE" }),
  backupDrReference: () => http<BackupDrReference>("/backupdr/reference"),
  updateBackupDrReference: (body: { types: BackupDrReference["types"]; reason?: string }) =>
    http<BackupDrReference>("/backupdr/reference", { method: "PUT", body: JSON.stringify(body) }),
  backupDrReferenceRevisions: () => http<{ revisions: BackupDrReferenceRevision[] }>("/backupdr/reference/revisions"),
  restoreBackupDrReference: (revision_id: string) =>
    http<{ ok: boolean; reference?: BackupDrReference }>("/backupdr/reference/restore", {
      method: "POST",
      body: JSON.stringify({ revision_id }),
    }),
  resetBackupDrReference: () =>
    http<{ ok: boolean; reference: BackupDrReference }>("/backupdr/reference/reset", { method: "POST", body: "{}" }),
  seedBackupDrDemo: () =>
    http<{ ok: boolean; workload_id: string; pct_protected: number }>("/backupdr/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Retirement & Breaking-Change Radar
  radarOverview: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<RadarSnapshot>(`/radar/overview?${q.toString()}`);
  },
  refreshRadar: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<RadarSnapshot>(`/radar/refresh?${q.toString()}`, { method: "POST", body: "{}" });
  },
  // --- Central knowledge graph (/graph) ---
  graphOverview: (connectionId = "") => {
    const q = new URLSearchParams();
    if (connectionId) q.set("connection_id", connectionId);
    return http<GraphOverview>(`/graph/overview?${q.toString()}`);
  },
  graphExpand: (nodeId: string, connectionId = "") =>
    http<GraphResult>("/graph/expand", {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId, connection_id: connectionId || null }),
    }),
  graphBuild: (scopeKind: string, scopeId: string, opts: { connectionId?: string; overlays?: string[]; drift?: boolean } = {}) =>
    http<GraphResult & { drift?: GraphDrift }>("/graph/build", {
      method: "POST",
      body: JSON.stringify({
        scope_kind: scopeKind,
        scope_id: scopeId,
        connection_id: opts.connectionId || null,
        overlays: opts.overlays || [],
        drift: !!opts.drift,
      }),
    }),
  graphBuildWorkloads: (workloadIds: string[], opts: { connectionId?: string; overlays?: string[]; drift?: boolean } = {}) =>
    http<GraphResult & { workload_ids: string[]; workload_count: number; drift_by_workload?: Record<string, GraphDrift> }>("/graph/workloads", {
      method: "POST",
      body: JSON.stringify({
        workload_ids: workloadIds,
        connection_id: opts.connectionId || null,
        overlays: opts.overlays || [],
        drift: !!opts.drift,
      }),
    }),
  graphSearch: (q: string, connectionId = "") => {
    const p = new URLSearchParams();
    p.set("q", q);
    if (connectionId) p.set("connection_id", connectionId);
    return http<{ nodes: GraphNode[]; query: string; count: number }>(`/graph/search?${p.toString()}`);
  },
  graphNode: (nodeId: string, connectionId = "") => {
    const p = new URLSearchParams();
    p.set("node_id", nodeId);
    if (connectionId) p.set("connection_id", connectionId);
    return http<GraphNodeDetail>(`/graph/node?${p.toString()}`);
  },
  graphPath: (nodes: GraphNode[], edges: GraphEdge[], source: string, target: string, directed = false) =>
    http<GraphPathResult>("/graph/path", { method: "POST", body: JSON.stringify({ nodes, edges, source, target, directed }) }),
  graphBlastRadius: (nodes: GraphNode[], edges: GraphEdge[], source: string, maxDepth = 3, directed = false) =>
    http<GraphBlastResult>("/graph/blast-radius", { method: "POST", body: JSON.stringify({ nodes, edges, source, max_depth: maxDepth, directed }) }),
  graphAnalytics: (connectionId = "") => {
    const p = new URLSearchParams();
    if (connectionId) p.set("connection_id", connectionId);
    return http<GraphAnalytics>(`/graph/analytics?${p.toString()}`);
  },
  graphDrift: (workloadId: string, connectionId = "") => {
    const p = new URLSearchParams();
    p.set("workload_id", workloadId);
    if (connectionId) p.set("connection_id", connectionId);
    return http<{ found: boolean; detail?: string; workload_id: string; workload_name: string; drift: GraphDrift }>(`/graph/drift?${p.toString()}`);
  },
  graphCompare: (scopeKind: string, leftId: string, rightId: string, connectionId = "") =>
    http<GraphCompare>("/graph/compare", { method: "POST", body: JSON.stringify({ scope_kind: scopeKind, left_id: leftId, right_id: rightId, connection_id: connectionId || null }) }),
  graphNarrative: (scopeKind = "overview", scopeId = "", connectionId = "") =>
    http<GraphNarrative>("/graph/narrative", { method: "POST", body: JSON.stringify({ scope_kind: scopeKind, scope_id: scopeId, connection_id: connectionId || null }) }),
  graphAsk: (question: string, connectionId = "") =>
    http<GraphAskResult>("/graph/ask", { method: "POST", body: JSON.stringify({ question, connection_id: connectionId || null }) }),
  graphViews: () => http<{ views: GraphView[] }>("/graph/views"),
  graphSaveView: (view: Partial<GraphView>) => http<{ view: GraphView }>("/graph/views", { method: "POST", body: JSON.stringify(view) }),
  graphDeleteView: (id: string) => http<{ ok: boolean }>(`/graph/views/${encodeURIComponent(id)}`, { method: "DELETE" }),
  graphPrefs: (tenantId = "") => http<{ layout: string; updated_at?: string }>(`/graph/prefs?tenant_id=${encodeURIComponent(tenantId)}`),
  graphSavePrefs: (tenantId: string, layout: string) =>
    http<{ layout: string; updated_at?: string }>("/graph/prefs", { method: "PUT", body: JSON.stringify({ tenant_id: tenantId, layout }) }),
  // --- Workload Mission Control ---
  missionSystems: () => http<{ systems: MissionSystemDef[] }>("/missions/systems"),
  missionState: (workloadId: string) =>
    http<MissionState>(`/missions/state?workload_id=${encodeURIComponent(workloadId)}`),
  runMission: (body: { workload_id: string; systems?: string[]; force?: boolean; connection_id?: string | null }) =>
    http<{ mission: Mission }>("/missions/run", { method: "POST", body: JSON.stringify(body) }),
  runFleet: (body: { workload_ids: string[]; systems?: string[]; force?: boolean; connection_id?: string | null }) =>
    http<{ missions: Mission[]; launched: number }>("/missions/fleet", { method: "POST", body: JSON.stringify(body) }),
  listMissions: (workloadId?: string, limit = 50) => {
    const q = new URLSearchParams();
    if (workloadId) q.set("workload_id", workloadId);
    q.set("limit", String(limit));
    return http<{ missions: Mission[] }>(`/missions?${q.toString()}`);
  },
  getMission: (id: string) => http<{ mission: Mission }>(`/missions/${encodeURIComponent(id)}`),
  cancelMission: (id: string) =>
    http<{ ok: boolean }>(`/missions/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" }),
  deleteMission: (id: string) =>
    http<{ ok: boolean }>(`/missions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteWorkloadMissions: (workloadId: string) =>
    http<{ ok: boolean; deleted: number }>(`/missions/workload/${encodeURIComponent(workloadId)}`, { method: "DELETE" }),
  radarRunbook: (body: { event: RadarEvent; architecture_id?: string }) =>
    http<{ ok: boolean; runbook: string; used_ai: boolean }>("/radar/runbook", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateRadarState: (body: {
    tracking_id: string;
    status?: string;
    assignee?: string;
    waive_reason?: string;
  }) =>
    http<{ ok: boolean; state?: Record<string, unknown>; detail?: string }>("/radar/state", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  registerRadarFindings: (body: { workload_id: string; workload_name: string; items: RadarEvent[] }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/radar/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createRadarTicket: (body: { connector_id: string; item: RadarEvent }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/radar/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  radarDigestPreview: (params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{
      lead_days: number[];
      events: Record<string, unknown>[];
      models: RadarModelItem[];
      new_count: number;
      approaching_count: number;
      summary: string;
    }>(`/radar/digest/preview?${q.toString()}`);
  },
  radarReference: () => http<RadarReference>("/radar/reference"),
  updateRadarReference: (body: {
    classification_rules: RadarClassificationRule[];
    model_lifecycle: RadarModelLifecycle[];
    reason?: string;
  }) => http<RadarReference>("/radar/reference", { method: "PUT", body: JSON.stringify(body) }),
  radarReferenceRevisions: () => http<{ revisions: RadarReferenceRevision[] }>("/radar/reference/revisions"),
  restoreRadarReference: (revision_id: string) =>
    http<{ ok: boolean; reference?: RadarReference }>("/radar/reference/restore", {
      method: "POST",
      body: JSON.stringify({ revision_id }),
    }),
  resetRadarReference: () =>
    http<{ ok: boolean; reference: RadarReference }>("/radar/reference/reset", { method: "POST", body: "{}" }),
  seedRadarDemo: () =>
    http<{ ok: boolean; workload_id: string; counts: Record<string, number> }>("/radar/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Telemetry Intelligence
  teleintelOverview: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<TeleIntelOverview>(`/teleintel/overview?${q.toString()}`);
  },
  teleintelQuery: (body: { kql: string; workload_id?: string; subscription_id?: string; component_id?: string }) =>
    http<{ ok: boolean; kql: string; rows: Record<string, unknown>[]; error?: string; path?: string }>(
      "/teleintel/query",
      { method: "POST", body: JSON.stringify(body) },
    ),
  teleintelTriage: (params: { workload_id?: string; subscription_id?: string; connection_id?: string; component_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    if (params.component_id) q.set("component_id", params.component_id);
    return http<TeleIntelTriage>(`/teleintel/triage?${q.toString()}`);
  },
  teleintelTimeline: (params: { workload_id?: string; subscription_id?: string; connection_id?: string; component_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    if (params.component_id) q.set("component_id", params.component_id);
    return http<TeleIntelTimeline>(`/teleintel/timeline?${q.toString()}`);
  },
  teleintelSmartDetection: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<TeleIntelSmartDetection>(`/teleintel/smart-detection?${q.toString()}`);
  },
  teleintelTransaction: (body: { operation_id: string; workload_id?: string; subscription_id?: string; connection_id?: string; component_id?: string }) =>
    http<TeleIntelTransaction>("/teleintel/transaction", { method: "POST", body: JSON.stringify(body) }),
  teleintelCodeOptimizations: (params: { workload_id?: string; subscription_id?: string; connection_id?: string; component_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    if (params.component_id) q.set("component_id", params.component_id);
    return http<TeleIntelCodeOpt>(`/teleintel/code-optimizations?${q.toString()}`);
  },
  registerTeleintelFinding: (body: { workload_id: string; workload_name: string; triage: TeleIntelTriage }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/teleintel/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createTeleintelTicket: (body: { connector_id: string; triage: TeleIntelTriage }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/teleintel/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  seedTeleintelDemo: () =>
    http<{ ok: boolean; workload_id: string; component: string }>("/teleintel/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Performance Profiler
  perfFleet: () => http<PerfFleet>("/performance/fleet"),
  // --- Cleanup tab (cross-scope) — one set per feature prefix ---
  cleanupList: (prefix: string) => http<CleanupData>(`${prefix}/cleanup`),
  cleanupTrash: (prefix: string, ids: string[]) =>
    http<CleanupResult>(`${prefix}/cleanup/trash`, { method: "POST", body: JSON.stringify({ ids }) }),
  cleanupRestore: (prefix: string, ids: string[]) =>
    http<CleanupResult>(`${prefix}/cleanup/restore`, { method: "POST", body: JSON.stringify({ ids }) }),
  cleanupPurge: (prefix: string, ids: string[]) =>
    http<CleanupResult>(`${prefix}/cleanup/purge`, { method: "POST", body: JSON.stringify({ ids }) }),
  perfProfile: (params: { workload_id?: string; subscription_id?: string; connection_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    if (params.connection_id) q.set("connection_id", params.connection_id);
    return http<PerfProfile>(`/performance/profile?${q.toString()}`);
  },
  perfResource: (params: { resource_id: string; run_id?: string; workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    q.set("resource_id", params.resource_id);
    if (params.run_id) q.set("run_id", params.run_id);
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ ok: boolean; resource?: PerfResourceRow; detail?: string }>(`/performance/resource?${q.toString()}`);
  },
  perfRuns: (params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ runs: PerfRunSummary[] }>(`/performance/runs?${q.toString()}`);
  },
  perfRun: (run_id: string) => http<{ ok: boolean; run?: PerfProfile; detail?: string }>(`/performance/run/${encodeURIComponent(run_id)}`),
  deletePerfRun: (run_id: string) => http<{ ok: boolean }>(`/performance/run/${encodeURIComponent(run_id)}`, { method: "DELETE" }),
  // Performance report — branded PDF download + Evidence Locker capture (a specific run, or the latest for a scope).
  perfRunPdf: (run_id: string, signal?: AbortSignal) =>
    httpBlob(`/performance/run/${encodeURIComponent(run_id)}/pdf`, { signal }),
  perfLatestPdf: (params: { workload_id?: string; subscription_id?: string }, signal?: AbortSignal) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return httpBlob(`/performance/pdf?${q.toString()}`, { signal });
  },
  perfRunEvidence: (run_id: string) =>
    http<{ ok: boolean; snapshot: { id: string; name: string; sha256: string } }>(
      `/performance/run/${encodeURIComponent(run_id)}/evidence`, { method: "POST", body: "{}" },
    ),
  perfLatestEvidence: (params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ ok: boolean; snapshot: { id: string; name: string; sha256: string } }>(
      `/performance/evidence?${q.toString()}`, { method: "POST", body: "{}" },
    );
  },
  perfTrashedRuns: (params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ runs: PerfRunSummary[] }>(`/performance/runs/trash?${q.toString()}`);
  },
  restorePerfRun: (run_id: string) => http<{ ok: boolean }>(`/performance/run/${encodeURIComponent(run_id)}/restore`, { method: "POST", body: "{}" }),
  purgePerfRun: (run_id: string) => http<{ ok: boolean }>(`/performance/run/${encodeURIComponent(run_id)}/purge`, { method: "DELETE" }),
  emptyPerfTrash: (params: { workload_id?: string; subscription_id?: string }) => {
    const q = new URLSearchParams();
    if (params.workload_id) q.set("workload_id", params.workload_id);
    if (params.subscription_id) q.set("subscription_id", params.subscription_id);
    return http<{ ok: boolean; deleted: number }>(`/performance/runs/trash/empty?${q.toString()}`, { method: "POST", body: "{}" });
  },
  registerPerfFindings: (body: { workload_id: string; workload_name: string; bottlenecks: PerfBottleneck[] }) =>
    http<{ ok: boolean; run_id: string; finding_count: number }>("/performance/findings/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createPerfTicket: (body: { connector_id: string; bottleneck: PerfBottleneck }) =>
    http<{ ok: boolean; connector_type?: string; ticket_id?: string; ticket_url?: string; detail?: string }>(
      "/performance/ticket",
      { method: "POST", body: JSON.stringify(body) },
    ),
  seedPerfDemo: () =>
    http<{ ok: boolean; workload_id: string; scorecard: Record<string, number> }>("/performance/demo/seed", {
      method: "POST",
      body: "{}",
    }),
  // Private Network Reachability Analyzer (netcheck)
  netcheckSources: (architecture_id: string) =>
    http<{ workload_id: string; fallback?: boolean; sources: NetCheckSource[] }>(`/netcheck/sources?architecture_id=${encodeURIComponent(architecture_id)}`),
  netcheckRuns: (architecture_id: string) =>
    http<{ runs: NetCheckRun[] }>(`/netcheck/runs?architecture_id=${encodeURIComponent(architecture_id)}`),
  netcheckRun: (id: string) => http<{ ok: boolean; run: NetCheckRun }>(`/netcheck/runs/${id}`),
  pinNetcheck: (body: { run_id: string; to_war_room?: boolean }) =>
    http<{ ok: boolean; pinned: boolean; detail: string }>("/netcheck/pin", { method: "POST", body: JSON.stringify(body) }),
  netcheckReport: (id: string) => http<{ ok: boolean; markdown: string; run: NetCheckRun }>(`/netcheck/report/${id}`),
  seedNetcheckDemo: () =>
    http<{ ok: boolean; run_id: string; verdict: string; diff: NetCheckDiff[] }>("/netcheck/demo/seed", { method: "POST", body: "{}" }),
  // Private Endpoint Resolution Debugger (dnsdebug)
  dnsdebugSources: (architecture_id: string) =>
    http<{ workload_id: string; fallback?: boolean; sources: DnsDebugSource[] }>(`/dnsdebug/sources?architecture_id=${encodeURIComponent(architecture_id)}`),
  dnsdebugRuns: (architecture_id: string) =>
    http<{ runs: DnsDebugRun[] }>(`/dnsdebug/runs?architecture_id=${encodeURIComponent(architecture_id)}`),
  dnsdebugRun: (id: string) => http<{ ok: boolean; run: DnsDebugRun }>(`/dnsdebug/runs/${id}`),
  dnsdebugIac: (run_id: string) => http<{ ok: boolean; iac: string; format: string }>("/dnsdebug/iac", { method: "POST", body: JSON.stringify({ run_id }) }),
  pinDnsdebug: (body: { run_id: string; to_war_room?: boolean }) =>
    http<{ ok: boolean; pinned: boolean; detail: string }>("/dnsdebug/pin", { method: "POST", body: JSON.stringify(body) }),
  dnsdebugReport: (id: string) => http<{ ok: boolean; markdown: string; run: DnsDebugRun }>(`/dnsdebug/report/${id}`),
  seedDnsdebugDemo: () =>
    http<{ ok: boolean; run_id: string; classification: string; diff: DnsDebugDiff[] }>("/dnsdebug/demo/seed", { method: "POST", body: "{}" }),
  // Evidence Locker
  evidenceList: (params?: { workload_id?: string; creator?: string; tag?: string; finding?: string; retention_class?: string }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v) q.set(k, v); });
    const qs = q.toString();
    return http<{ snapshots: EvidenceSnapshot[] }>(`/evidence${qs ? `?${qs}` : ""}`);
  },
  createEvidence: (body: EvidenceCreateRequest) =>
    http<{ ok: boolean; snapshot: EvidenceSnapshot }>("/evidence", { method: "POST", body: JSON.stringify(body) }),
  evidenceDetail: (id: string) =>
    http<{ ok: boolean; snapshot: EvidenceSnapshot; sha_verified: boolean }>(`/evidence/${id}`),
  evidenceContent: (id: string, tab?: string) =>
    http<{ ok: boolean; tab?: string; content: Record<string, unknown> }>(`/evidence/${id}/content${tab ? `?tab=${tab}` : ""}`),
  evidenceDiff: (body: { a: string; b: string; type_filter?: string; tag_filter?: string; finding_filter?: string }) =>
    http<{ ok: boolean; a: { id: string; name: string; created_at: string }; b: { id: string; name: string; created_at: string }; diff: EvidenceDiff }>(
      "/evidence/diff", { method: "POST", body: JSON.stringify(body) }),
  attachEvidence: (id: string, body: { target: string; connector_id?: string; note?: string }) =>
    http<{ ok: boolean; ticket_id?: string; ticket_url?: string; detail?: string; attached?: string; body?: string }>(
      `/evidence/${id}/attach`, { method: "POST", body: JSON.stringify(body) }),
  shareEvidence: (id: string, ttl_days = 30) =>
    http<{ ok: boolean; share: { token: string; expires_at: string } }>(`/evidence/${id}/share`, { method: "POST", body: JSON.stringify({ ttl_days }) }),
  exportEvidence: (id: string) =>
    http<{ ok: boolean; bundle: { meta: EvidenceSnapshot; content: Record<string, unknown>; sha_verified: boolean } }>(`/evidence/${id}/export`),
  evidenceTrash: () => http<{ snapshots: EvidenceSnapshot[] }>("/evidence/trash"),
  deleteEvidence: (id: string) => http<{ ok: boolean; snapshot?: EvidenceSnapshot; detail?: string }>(`/evidence/${id}`, { method: "DELETE" }),
  restoreEvidence: (id: string) => http<{ ok: boolean; snapshot?: EvidenceSnapshot; detail?: string }>(`/evidence/${id}/restore`, { method: "POST", body: "{}" }),
  purgeEvidence: (id: string) => http<{ ok: boolean }>(`/evidence/${id}/purge`, { method: "DELETE" }),
  emptyEvidenceTrash: () => http<{ ok: boolean; purged: number }>("/evidence/trash/empty", { method: "POST", body: "{}" }),
  seedEvidenceDemo: () =>
    http<{ ok: boolean; before_id: string; after_id: string }>("/evidence/demo/seed", { method: "POST", body: "{}" }),
  // Custom checks
  assessmentCustomChecks: () => http<{ checks: AssessmentCheckMeta[] }>("/assessments/custom-checks"),
  upsertAssessmentCustomCheck: (body: Partial<AssessmentCheckMeta> & { id?: string }) =>
    http<{ check: AssessmentCheckMeta }>("/assessments/custom-checks", { method: "PUT", body: JSON.stringify(body) }),
  deleteAssessmentCustomCheck: (id: string) =>
    http<{ ok: boolean }>(`/assessments/custom-checks/${id}`, { method: "DELETE" }),
  generateAssessmentCheck: (goal: string) =>
    http<{ draft: Partial<AssessmentCheckMeta> }>("/assessments/custom-checks/generate", {
      method: "POST",
      body: JSON.stringify({ goal }),
    }),
  // Schedules
  assessmentSchedules: () => http<{ schedules: AssessmentSchedule[] }>("/assessments/schedules"),
  upsertAssessmentSchedule: (body: Partial<AssessmentSchedule> & { id?: string }) =>
    http<{ schedule: AssessmentSchedule }>("/assessments/schedules", { method: "PUT", body: JSON.stringify(body) }),
  deleteAssessmentSchedule: (id: string) =>
    http<{ ok: boolean }>(`/assessments/schedules/${id}`, { method: "DELETE" }),

  // --- Azure Policy ---
  policyBaselines: () => http<{ baselines: PolicyBaseline[] }>("/policy/baselines"),
  policyInventory: (connectionId?: string | null, withCompliance = false, force = false, workloadId?: string | null) =>
    http<PolicyInventory>(
      `/policy/inventory?with_compliance=${withCompliance ? 1 : 0}&force=${force ? 1 : 0}` +
        (connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : "") +
        (workloadId ? `&workload_id=${encodeURIComponent(workloadId)}` : ""),
    ),
  policyCompliance: (connectionId?: string | null) =>
    http<PolicyCompliance>("/policy/compliance" + (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "")),
  policyEffective: (scope: string, assignments: PolicyAssignment[], exemptions: PolicyExemption[]) =>
    http<PolicyEffective>("/policy/effective", { method: "POST", body: JSON.stringify({ scope, assignments, exemptions }) }),
  policyWhatIf: (policyJson: string, connectionId?: string | null, displayName = "Candidate policy") =>
    http<PolicyWhatIf>("/policy/whatif", {
      method: "POST",
      body: JSON.stringify({ policy_json: policyJson, connection_id: connectionId ?? null, display_name: displayName }),
    }),
  policyAuthor: (intent: string) =>
    http<PolicyAuthorResult>("/policy/author", { method: "POST", body: JSON.stringify({ intent }) }),
  policyExplain: (policyJson: string) =>
    http<{ explanation: string }>("/policy/explain", { method: "POST", body: JSON.stringify({ policy_json: policyJson }) }),
  policyTriage: (errorText: string, candidates: PolicyAssignment[]) =>
    http<PolicyTriageResult>("/policy/triage", { method: "POST", body: JSON.stringify({ error_text: errorText, candidates }) }),
  policyCoverage: (
    baselineId: string,
    assignments: PolicyAssignment[],
    definitions: PolicyDefinition[],
    withProposals = true,
    workloadId = "",
    workloadName = "",
    connectionId = "",
  ) =>
    http<PolicyCoverage>("/policy/coverage", {
      method: "POST",
      body: JSON.stringify({
        baseline_id: baselineId,
        assignments,
        definitions,
        with_proposals: withProposals,
        workload_id: workloadId,
        workload_name: workloadName,
        connection_id: connectionId,
      }),
    }),
  policyCoverageRuns: (workloadId?: string | null) =>
    http<{ runs: PolicyCoverageRunSummary[] }>(
      "/policy/coverage-runs" + (workloadId ? `?workload_id=${encodeURIComponent(workloadId)}` : ""),
    ),
  policyCoverageRun: (id: string) =>
    http<{ run: PolicyCoverageRunRecord }>(`/policy/coverage-runs/${encodeURIComponent(id)}`),
  policyDeleteCoverageRun: (id: string) =>
    http<{ ok: boolean }>(`/policy/coverage-runs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  policyRollout: (intent: string, policyJson = "") =>
    http<PolicyRolloutPlan>("/policy/rollout", { method: "POST", body: JSON.stringify({ intent, policy_json: policyJson }) }),
  policySimulate: (body: PolicySimulateReq) =>
    http<PolicySimulateResult>("/policy/simulate", { method: "POST", body: JSON.stringify(body) }),
  policySimulations: (workloadId?: string | null) =>
    http<{ simulations: PolicySimulationSummary[] }>(
      "/policy/simulations" + (workloadId ? `?workload_id=${encodeURIComponent(workloadId)}` : ""),
    ),
  policySaveSimulation: (result: PolicySimulateResult, workloadId = "", workloadName = "", connectionId = "") =>
    http<{ simulation: PolicySimulationSummary }>("/policy/simulations", {
      method: "POST",
      body: JSON.stringify({ result, workload_id: workloadId, workload_name: workloadName, connection_id: connectionId }),
    }),
  policySimulation: (id: string) =>
    http<{ simulation: PolicySimulationRecord }>(`/policy/simulations/${encodeURIComponent(id)}`),
  policyDeleteSimulation: (id: string) =>
    http<{ ok: boolean }>(`/policy/simulations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  policyEnforcementLinks: (workloadId?: string | null) =>
    http<{ links: PolicyEnforcementLink[] }>(
      "/policy/enforcement-links" + (workloadId ? `?workload_id=${encodeURIComponent(workloadId)}` : ""),
    ),
  policySaveEnforcementLink: (body: Partial<PolicyEnforcementLink> & { check_id: string }) =>
    http<{ link: PolicyEnforcementLink }>("/policy/enforcement-link", { method: "POST", body: JSON.stringify(body) }),  policyTagGovernance: (requiredTags: string[], connectionId?: string | null) =>
    http<PolicyTagGovernance>("/policy/tag-governance", {
      method: "POST",
      body: JSON.stringify({ required_tags: requiredTags, connection_id: connectionId ?? null }),
    }),
  policyIacSource: () => http<PolicyIacSource>("/policy/iac-source"),
  policySetIacSource: (content: string, format: string) =>
    http<PolicyIacSource>("/policy/iac-source", { method: "PUT", body: JSON.stringify({ content, format }) }),
  policyDrift: (assignments: PolicyAssignment[]) =>
    http<PolicyDriftResult>("/policy/drift", { method: "POST", body: JSON.stringify({ assignments }) }),
  // Exemption manager — plan (preview), apply (create/update), remove (delete), guardrails.
  policyExemptionGuardrails: () =>
    http<{ guardrails: PolicyExemptionGuardrails }>("/policy/exemption/guardrails"),
  policyExemptionPlan: (action: "create" | "update", payload: PolicyExemptionPayload) =>
    http<PolicyExemptionPlan>("/policy/exemption/plan", {
      method: "POST",
      body: JSON.stringify({ action, payload }),
    }),
  policyExemptionApply: (action: "create" | "update", payload: PolicyExemptionPayload, connectionId?: string | null) =>
    http<{ ok: boolean; resource?: Record<string, unknown>; status?: number; plan: PolicyExemptionPlan }>(
      "/policy/exemption/apply",
      { method: "POST", body: JSON.stringify({ action, payload, connection_id: connectionId ?? null }) },
    ),
  policyExemptionRemove: (id: string, connectionId?: string | null) =>
    http<{ ok: boolean; status?: number }>("/policy/exemption/remove", {
      method: "POST",
      body: JSON.stringify({ id, connection_id: connectionId ?? null }),
    }),
  // Excel export — POST sheets (flat tables and/or pivot trees with outline levels) → .xlsx blob.
  policyExportXlsx: (filename: string, sheets: PolicyXlsxSheet[]) =>
    httpBlob("/policy/export/xlsx", { method: "POST", body: JSON.stringify({ filename, sheets }) }),
  policySnapshots: () => http<{ snapshots: PolicySnapshot[] }>("/policy/snapshots"),
  policyTakeSnapshot: (connectionId?: string | null, withCompliance = true) =>
    http<{ snapshot: PolicySnapshot; drift_since_previous: PolicyDrift | null }>("/policy/snapshot", {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId ?? null, with_compliance: withCompliance }),
    }),
  policyDrafts: () => http<{ drafts: PolicyDraft[] }>("/policy/drafts"),
  policySaveDraft: (draft: Partial<PolicyDraft>) =>
    http<PolicyDraft>("/policy/drafts", { method: "PUT", body: JSON.stringify(draft) }),
  policyDeleteDraft: (id: string) => http<{ ok: boolean }>(`/policy/drafts/${id}`, { method: "DELETE" }),

  // --- Inventory ---
  inventory: (connectionId?: string | null, force = false) =>
    http<InventoryResponse>(
      `/inventory?force=${force ? 1 : 0}` + (connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),
  inventoryNlSearch: (body: {
    query: string;
    connection_id?: string;
    types?: string[];
    locations?: string[];
    workloads?: string[];
    subscriptions?: string[];
  }, signal?: AbortSignal) => http<InventoryNlResult>("/inventory/nl-search", { method: "POST", body: JSON.stringify(body), signal }),
  inventoryExplain: (resource: InventoryResource) =>
    http<{ explanation: string }>("/inventory/explain", { method: "POST", body: JSON.stringify({ resource }) }),
  inventoryInsights: (connectionId?: string | null) =>
    http<InventoryInsights>("/inventory/insights" + (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "")),
  inventorySnapshots: (connectionId?: string | null) =>
    http<{ snapshots: InventorySnapshot[] }>("/inventory/snapshots" + (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "")),
  inventoryTakeSnapshot: (connectionId?: string | null) =>
    http<{ snapshot: InventorySnapshot; drift_since_previous: InventoryDrift | null }>(
      "/inventory/snapshots" + (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
      { method: "POST" },
    ),
  inventoryDrift: (connectionId?: string | null, baselineId?: string) =>
    http<{ drift: InventoryDrift | null; reason?: string }>(
      "/inventory/drift?" +
        new URLSearchParams({
          ...(connectionId ? { connection_id: connectionId } : {}),
          ...(baselineId ? { baseline_id: baselineId } : {}),
        }).toString(),
    ),
  inventoryDeleteSnapshot: (id: string) =>
    http<{ ok: boolean }>(`/inventory/snapshots/${encodeURIComponent(id)}`, { method: "DELETE" }),
  inventoryGovernance: (resourceId: string, connectionId?: string | null) =>
    http<{ effective: InventoryGovernance }>("/inventory/governance", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId, connection_id: connectionId ?? "" }),
    }),
  inventoryFindings: (resourceId: string) =>
    http<{ findings: InventoryFinding[]; count: number }>("/inventory/findings", {
      method: "POST",
      body: JSON.stringify({ resource_id: resourceId }),
    }),
  inventoryCost: (connectionId?: string | null, force = false, cachedOnly = false) =>
    http<InventoryCost>(
      `/inventory/cost?force=${force ? 1 : 0}&cached_only=${cachedOnly ? 1 : 0}` + (connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),
  inventoryCostRollup: (connectionId?: string | null, force = false, cachedOnly = false) =>
    http<InventoryCostRollup>(
      `/inventory/cost-rollup?force=${force ? 1 : 0}&cached_only=${cachedOnly ? 1 : 0}` + (connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),
  inventoryOptimization: (connectionId?: string | null) =>
    http<InventoryOptimization>(
      "/inventory/optimization" + (connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""),
    ),

  // --- Tag Intelligence (F1-F12) ---
  tagintelCensus: (sel: TagScopeSel, force = false) =>
    http<TagCensusResponse>(`/tagintel/census?${tagQ(sel, { force: force ? "1" : "0" })}`),
  tagintelCensusDrill: (sel: TagScopeSel, q: { key: string; value?: string; subscription_id?: string; resource_type?: string; fold_casing?: boolean }) =>
    http<TagDrillResponse>(`/tagintel/census/drill?${tagQ(sel, {
      key: q.key,
      ...(q.value !== undefined ? { value: q.value } : {}),
      ...(q.subscription_id !== undefined ? { subscription_id: q.subscription_id } : {}),
      ...(q.resource_type !== undefined ? { resource_type: q.resource_type } : {}),
      fold_casing: q.fold_casing === false ? "0" : "1",
    })}`),
  tagintelAsk: (body: { question: string } & TagScopeSel, signal?: AbortSignal) =>
    http<TagAskResponse>("/tagintel/ask", { method: "POST", body: JSON.stringify(tagBody(body)), signal }),
  tagintelGenerate: (body: { question: string } & TagScopeSel, signal?: AbortSignal) =>
    http<TagGenerateResponse>("/tagintel/generate", { method: "POST", body: JSON.stringify(tagBody(body)), signal }),
  tagintelHygiene: (sel: TagScopeSel) =>
    http<TagHygieneResponse>(`/tagintel/hygiene?${tagQ(sel)}`),
  tagintelCatalog: () => http<{ entries: TagCatalogEntry[] }>("/tagintel/catalog"),
  tagintelCatalogSave: (entry: Partial<TagCatalogEntry>) =>
    http<TagCatalogEntry>("/tagintel/catalog", { method: "POST", body: JSON.stringify(entry) }),
  tagintelCatalogDelete: (id: string) =>
    http<{ deleted: boolean }>(`/tagintel/catalog/${encodeURIComponent(id)}`, { method: "DELETE" }),
  tagintelCatalogSeed: (sel: TagScopeSel, limit = 12) =>
    http<{ available: boolean; created: TagCatalogEntry[]; entries: TagCatalogEntry[] }>(
      "/tagintel/catalog/seed", { method: "POST", body: JSON.stringify(tagBody({ ...sel, limit })) }),
  tagintelCoverage: (sel: TagScopeSel, required?: string) =>
    http<TagCoverageResponse>(`/tagintel/coverage?${tagQ(sel, required ? { required } : {})}`),
  tagintelCost: (sel: TagScopeSel, dimension = "workload") =>
    http<TagCostResponse>(`/tagintel/cost?${tagQ(sel, { dimension })}`),
  tagintelBillingMap: (sel: TagScopeSel) =>
    http<TagBillingMapResponse>(`/tagintel/billing-map?${tagQ(sel)}`),
  tagintelCmdbReconcile: (sel: TagScopeSel, cmdb_codes: string[]) =>
    http<TagCmdbReconcile>("/tagintel/cmdb-reconcile", { method: "POST", body: JSON.stringify(tagBody({ ...sel, cmdb_codes })) }),
  tagintelDrift: (sel: TagScopeSel) =>
    http<{ snapshots: TagDriftSnapshot[] }>(`/tagintel/drift?${tagQ(sel)}`),
  tagintelDriftSnapshot: (sel: TagScopeSel) =>
    http<{ available: boolean; snapshot?: TagDriftSnapshot }>(`/tagintel/drift/snapshot?${tagQ(sel)}`, { method: "POST" }),
  tagintelDriftDiff: (sel: TagScopeSel, base: string, head: string) =>
    http<TagDriftDiff>(`/tagintel/drift/diff?${tagQ(sel, { base, head })}`),
  tagintelPolicygen: (selections: { tag: string; effect: string; default_value?: string }[]) =>
    http<TagPolicyGenResponse>("/tagintel/policygen", { method: "POST", body: JSON.stringify({ selections }) }),
  tagintelPolicyLadder: () => http<{ ladder: TagPolicyLadderStep[] }>("/tagintel/policy/ladder"),
  tagintelRemediatePreview: (sel: TagScopeSel, op: TagRemediationOp) =>
    http<TagRemediationPlan>("/tagintel/remediate/preview", { method: "POST", body: JSON.stringify(tagBody({ ...sel, op })) }),
  tagintelRemediateScripts: (sel: TagScopeSel, op: TagRemediationOp) =>
    http<TagRemediationScripts>("/tagintel/remediate/scripts", { method: "POST", body: JSON.stringify(tagBody({ ...sel, op })) }),
  // Multi-operation change-set variants (preload/save flow).
  tagintelRemediatePreviewSet: (sel: TagScopeSel, operations: TagRemediationOp[]) =>
    http<TagRemediationPlan>("/tagintel/remediate/preview", { method: "POST", body: JSON.stringify(tagBody({ ...sel, operations })) }),
  tagintelRemediateScriptsSet: (sel: TagScopeSel, operations: TagRemediationOp[]) =>
    http<TagRemediationScripts>("/tagintel/remediate/scripts", { method: "POST", body: JSON.stringify(tagBody({ ...sel, operations })) }),
  tagintelRemediateApply: (sel: TagScopeSel, operations: TagRemediationOp[], changeset_id?: string) =>
    http<TagApplyResult>("/tagintel/remediate/apply", { method: "POST", body: JSON.stringify(tagBody({ ...sel, operations, approved: true, changeset_id: changeset_id || "" })) }),
  tagintelChangesets: () => http<{ changesets: TagChangeSet[]; groups: TagChangeSetGroup[] }>("/tagintel/changesets"),
  tagintelChangesetSave: (cs: { id?: string; name: string; description?: string; group_id?: string; labels?: string[]; operations: TagRemediationOp[] }) =>
    http<TagChangeSet>("/tagintel/changesets", { method: "POST", body: JSON.stringify(cs) }),
  tagintelChangesetDelete: (id: string) =>
    http<{ deleted: boolean }>(`/tagintel/changesets/${encodeURIComponent(id)}`, { method: "DELETE" }),
  tagintelChangesetDuplicate: (id: string) =>
    http<TagChangeSet>(`/tagintel/changesets/${encodeURIComponent(id)}/duplicate`, { method: "POST" }),
  tagintelChangesetMove: (id: string, group_id: string) =>
    http<TagChangeSet>(`/tagintel/changesets/${encodeURIComponent(id)}/move`, { method: "POST", body: JSON.stringify({ group_id }) }),
  tagintelChangesetGroups: () => http<{ groups: TagChangeSetGroup[] }>("/tagintel/changeset-groups"),
  tagintelChangesetGroupSave: (g: { id?: string; name: string; color?: string; description?: string; order?: number }) =>
    http<TagChangeSetGroup>("/tagintel/changeset-groups", { method: "POST", body: JSON.stringify(g) }),
  tagintelChangesetGroupDelete: (id: string) =>
    http<{ deleted: boolean }>(`/tagintel/changeset-groups/${encodeURIComponent(id)}`, { method: "DELETE" }),
  tagintelChangesetsExport: (ids?: string[]) =>
    http<TagChangeSetBundle>(`/tagintel/changesets/export${ids?.length ? `?ids=${encodeURIComponent(ids.join(","))}` : ""}`),
  tagintelChangesetsImport: (bundle: TagChangeSetBundle) =>
    http<TagChangeSetImportResult>("/tagintel/changesets/import", { method: "POST", body: JSON.stringify(bundle) }),
  tagintelRbacAdvice: () => http<TagRbacAdvice>("/tagintel/rbac-advice"),
  tagintelSummary: (sel: TagScopeSel) =>
    http<TagSummary>(`/tagintel/summary?${tagQ(sel)}`),

  // --- Change Explorer ---
  changeExplorerWorkloads: () => http<{ workloads: ChangeWorkload[] }>("/changeexplorer/workloads"),
  changeExplorerFleet: () => http<ChangeFleet>("/changeexplorer/fleet"),
  changeExplorerAnalyze: (body: {
    workload_id?: string; subscription_id?: string; subscription_name?: string;
    connection_id?: string; start_time: string; end_time: string; scope_mode: string;
  }) => http<ChangeAnalysisRun>("/changeexplorer/analyze", { method: "POST", body: JSON.stringify(body) }),
  changeExplorerRuns: (workloadId: string) =>
    http<{ runs: ChangeRunSummary[] }>(`/changeexplorer/runs?workload_id=${encodeURIComponent(workloadId)}`),
  changeExplorerRun: (runId: string) => http<ChangeAnalysisRun>(`/changeexplorer/runs/${encodeURIComponent(runId)}`),
  changeExplorerChangeRaw: (runId: string, changeId: string) =>
    http<{ rawEventJson: Record<string, unknown> | null }>(`/changeexplorer/runs/${encodeURIComponent(runId)}/changes/${encodeURIComponent(changeId)}/raw`),
  changeExplorerAsk: (body: { question: string; run_id?: string; workload_id?: string }, signal?: AbortSignal) =>
    http<ChangeAskResponse>("/changeexplorer/ask", { method: "POST", body: JSON.stringify(body), signal }),
  changeExplorerDeleteRun: (runId: string) =>
    http<{ deleted: boolean }>(`/changeexplorer/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }),
  changeExplorerTrash: (workloadId: string) =>
    http<{ runs: ChangeRunSummary[] }>(`/changeexplorer/runs/trash?workload_id=${encodeURIComponent(workloadId)}`),
  changeExplorerRestoreRun: (runId: string) =>
    http<{ restored: boolean }>(`/changeexplorer/runs/${encodeURIComponent(runId)}/restore`, { method: "POST", body: "{}" }),
  changeExplorerPurgeRun: (runId: string) =>
    http<{ purged: boolean }>(`/changeexplorer/runs/${encodeURIComponent(runId)}/purge`, { method: "DELETE" }),
  changeExplorerExport: (runId: string, format: string) =>
    http<{ filename?: string; mime?: string; content?: string; queries?: Record<string, string> }>(
      `/changeexplorer/runs/${encodeURIComponent(runId)}/export?format=${encodeURIComponent(format)}`),
  changeExplorerCompare: (runId: string, otherId: string) =>
    http<ChangeCompareResult>(`/changeexplorer/runs/${encodeURIComponent(runId)}/compare/${encodeURIComponent(otherId)}`),
  changeExplorerSetCase: (runId: string, body: { pinned?: string[]; notes?: Record<string, string>; case_summary?: string }) =>
    http<{ caseFile: ChangeCaseFile }>(`/changeexplorer/runs/${encodeURIComponent(runId)}/case`, { method: "POST", body: JSON.stringify(body) }),
  changeExplorerReportPdfUrl: (runId: string) => `${API_BASE}/changeexplorer/runs/${encodeURIComponent(runId)}/report.pdf`,
};

export interface TagScopeSel {
  connection_id?: string | null;
  scope?: string;
  workload_id?: string;
}
function tagQ(sel: TagScopeSel, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams();
  if (sel.connection_id) p.set("connection_id", sel.connection_id);
  if (sel.scope) p.set("scope", sel.scope);
  if (sel.workload_id) p.set("workload_id", sel.workload_id);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== "") p.set(k, v);
  return p.toString();
}
function tagBody<T extends TagScopeSel>(body: T): T {
  return { ...body, connection_id: body.connection_id ?? "", scope: body.scope ?? "", workload_id: body.workload_id ?? "" };
}

// --- Tag Intelligence types ---
export interface TagCensusKey {
  key: string;
  count: number;
  coverage_pct: number;
  subscription_count: number;
  distinct_values: number;
  category: string;
  high_cardinality: boolean;
  single_subscription: boolean;
  top_values: { value: string; count: number }[];
  casing_variants: string[];
}
export interface TagCensus {
  total_resources: number;
  tagged_count: number;
  untagged_count: number;
  tag_coverage_pct: number;
  distinct_keys: number;
  distinct_pairs: number;
  keys: TagCensusKey[];
  scope_coverage: {
    by_subscription: { id: string; name: string; total: number; tagged: number; coverage_pct: number }[];
    by_resource_group: { key: string; total: number; tagged: number; coverage_pct: number }[];
  };
  untagged_sample: { id: string; name: string; type: string; resource_group: string; subscription_id: string }[];
  category_breakdown: { category: string; count: number }[];
  flags: { high_cardinality: number; single_subscription: number };
}
export interface TagCensusResponse {
  available: boolean;
  never_loaded?: boolean;
  fetched_at: string;
  age_seconds?: number;
  truncated?: boolean;
  estate_cap?: number;
  census?: TagCensus;
}
// Power-BI-style lazy drill: key → value → subscription → resource type → resource.
export interface TagDrillRow {
  // value level
  value?: string;
  subscription_count?: number;
  distinct_types?: number;
  // subscription level
  subscription_id?: string;
  name?: string;
  // type level
  type?: string;
  // resource (leaf) level
  id?: string;
  resource_group?: string;
  location?: string;
  // common
  count?: number;
}
export interface TagDrillResponse {
  available: boolean;
  never_loaded?: boolean;
  level?: "value" | "subscription" | "type" | "resource";
  key?: string;
  value?: string;
  subscription_id?: string;
  resource_type?: string;
  rows?: TagDrillRow[];
  total?: number;
  truncated?: boolean;
  estate_truncated?: boolean;
}
export interface TagAskResponse {
  available?: boolean;
  kind: string;
  answer: string;
  data?: unknown[];
  key?: string;
  generated_query?: string;
  source?: string;
  needs_ai?: boolean;
}
export interface TagKeyCluster {
  canonical: string;
  members: string[];
  counts: Record<string, number>;
  affected: number;
  confidence: string;
  reason: string;
  category: string;
}
export interface TagValueCluster {
  key: string;
  category: string;
  distinct_values: number;
  variants: { canonical: string; members: string[]; affected: number; confidence: string }[];
}
export interface TagGroupingGroup {
  id: string;
  label: string;
  signal: string;
  confidence: string;
  resource_count: number;
  subscription_count: number;
  top_types: { type: string; count: number }[];
  needs_review: boolean;
}
export interface TagHygieneResponse {
  available: boolean;
  never_loaded?: boolean;
  fetched_at?: string;
  truncated?: boolean;
  key_clusters?: TagKeyCluster[];
  value_clusters?: TagValueCluster[];
  grouping?: {
    confirmed_resources: number;
    inferred_groups: TagGroupingGroup[];
    summary: { confirmed: number; high: number; medium: number; low: number };
  };
}
export interface TagCatalogEntry {
  id: string;
  canonical: string;
  aliases: string[];
  category: string;
  purpose: string;
  required: boolean;
  inherited: boolean;
  scope: string;
  allowed_values: string[];
  example_values: string[];
  owner: string;
  description: string;
  created_at?: string;
  updated_at?: string;
}
export interface TagCoverageResponse {
  available: boolean;
  never_loaded?: boolean;
  needs_required?: boolean;
  message?: string;
  fetched_at?: string;
  required?: string[];
  total_resources?: number;
  evaluated?: number;
  exempt?: number;
  compliant?: number;
  non_compliant?: number;
  coverage_pct?: number;
  per_key?: { key: string; missing: number; present: number; coverage_pct: number }[];
  missing_one?: { key: string; count: number; resources: { id: string; name: string; type: string; resource_group: string; subscription_id: string }[] }[];
  missing_one_total?: number;
  matrix?: { key: string; subscription: string; resource_group: string; total: number; missing: Record<string, number>; compliant_pct: number }[];
}
export interface TagCostResponse {
  available: boolean;
  never_loaded?: boolean;
  cost_available?: boolean;
  message?: string;
  dimension?: string;
  currency?: string;
  total_cost?: number;
  allocatable_cost?: number;
  unallocatable_cost?: number;
  allocatable_pct?: number;
  tagged_cost?: number;
  untagged_cost?: number;
  breakdown?: { label: string; cost: number }[];
  unallocatable_resources?: { id: string; name: string; type: string; cost: number; resource_group: string }[];
  shared_candidates?: { id: string; name: string; cost: number; workloads: string[] }[];
}
export interface TagBillingMapResponse {
  available: boolean;
  never_loaded?: boolean;
  cost_available?: boolean;
  currency?: string;
  total_codes?: number;
  rows?: {
    billing_code: string;
    cost: number;
    resource_count: number;
    subscription_count: number;
    workloads: { name: string; count: number }[];
    owners: { name: string; count: number }[];
    owner_coverage_pct: number;
    unallocated: boolean;
  }[];
}
export interface TagCmdbReconcile {
  available?: boolean;
  in_both: string[];
  only_in_azure: string[];
  only_in_cmdb: string[];
  match_pct: number;
}
export interface TagDriftSnapshot {
  id: string;
  taken_at: string;
  actor: string;
  resource_count: number;
  distinct_keys: number;
  coverage_pct: number;
}
export interface TagDriftKeyDetail {
  key: string;
  count: number;
  resources: { id: string; name: string }[];
}
export interface TagDriftChangedResource {
  id: string;
  name: string;
  added: { key: string; to?: unknown }[];
  removed: { key: string; from?: unknown }[];
  changed: { key: string; from?: unknown; to?: unknown }[];
}
export interface TagDriftDiff {
  error?: string;
  base?: TagDriftSnapshot;
  head?: TagDriftSnapshot;
  added_keys?: string[];
  removed_keys?: string[];
  added_key_details?: TagDriftKeyDetail[];
  removed_key_details?: TagDriftKeyDetail[];
  value_changes?: { id: string; name?: string; key: string; from: unknown; to: unknown }[];
  value_change_count?: number;
  billing_changes?: { id: string; name?: string; key: string; from: unknown; to: unknown }[];
  added_resources?: { id: string; name: string }[];
  removed_resources?: { id: string; name: string }[];
  changed_resources?: TagDriftChangedResource[];
  changed_resource_count?: number;
  coverage_delta?: number;
  resource_delta?: number;
}
export interface TagPolicyDefinition {
  name: string;
  properties: Record<string, unknown>;
  _effect: string;
  _tag: string;
}
export interface TagPolicyGenResponse {
  definitions: TagPolicyDefinition[];
  initiative: { name: string; properties: Record<string, unknown> };
  warnings: string[];
}
export interface TagPolicyLadderStep {
  phase: number;
  name: string;
  effect: string | null;
  description: string;
  risk: string;
}
export interface TagRemediationOp {
  type: "add_tag" | "set_tag" | "rename_key" | "normalize_value" | "remove_key";
  key?: string;
  value?: string;
  to_key?: string;
  from_value?: string;
  to_value?: string;
  resource_ids?: string[];
}
// AI Tag Generator: a proposed op carries the same shape plus the AI's rationale and how many
// resources it resolved to (so the review UI can show grounding before handoff to Remediate).
export interface TagGeneratedOp extends TagRemediationOp {
  rationale?: string;
  match_count?: number;
}
export interface TagGenerateResponse {
  available: boolean;
  summary?: string;
  operations?: TagGeneratedOp[];
  notes?: string[];
  answer?: string;
}
export interface TagRemediationItem {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  subscription_id: string;
  before: Record<string, string>;
  after: Record<string, string>;
  overwrite: boolean;
}
export interface TagRemediationPlan {
  available?: boolean;
  never_loaded?: boolean;
  op?: TagRemediationOp;
  items?: TagRemediationItem[];
  count?: number;
  overwrites?: number;
  subscription_count?: number;
  generated_at?: string;
}
export interface TagRemediationScripts {
  available?: boolean;
  count?: number;
  overwrites?: number;
  scripts?: { powershell: string; azcli: string; arg: string; rollback: string };
}
export interface TagChangeSet {
  id: string;
  name: string;
  description: string;
  group_id?: string;
  labels?: string[];
  operations: TagRemediationOp[];
  op_breakdown?: Record<string, number>;
  affected_keys?: string[];
  actor?: string;
  created_at?: string;
  updated_at?: string;
  run_count?: number;
  last_run?: { scope?: string; actor?: string; applied?: number; failed?: number; total?: number; at?: string } | null;
}
export interface TagChangeSetGroup {
  id: string;
  name: string;
  color: string;
  description: string;
  order: number;
  count?: number;
  created_at?: string;
  updated_at?: string;
}
/** Portable change-set library bundle (export download / import upload). */
export interface TagChangeSetBundle {
  kind?: string;
  version?: number;
  exported_at?: string;
  groups: { id?: string; name: string; color?: string; description?: string; order?: number }[];
  changesets: { name: string; description?: string; group_id?: string; labels?: string[]; operations: TagRemediationOp[] }[];
}
export interface TagChangeSetImportResult {
  imported: number;
  groups_created: number;
  skipped: number;
  errors: string[];
}
export interface TagApplyResult {
  available?: boolean;
  never_loaded?: boolean;
  count?: number;
  overwrites?: number;
  applied?: number;
  failed?: number;
  total?: number;
  blocked?: boolean;
  reason?: string;
  results?: { id: string; name: string; ok: boolean; error: string }[];
}
export interface TagRbacAdvice {
  rows: { action: string; role: string; scope: string; why: string; role_definition_id: string; assignment_example: string }[];
  principle: string;
}
export interface TagSummary {
  available: boolean;
  never_loaded?: boolean;
  fetched_at?: string;
  truncated?: boolean;
  total_resources?: number;
  tag_coverage_pct?: number;
  distinct_keys?: number;
  untagged_count?: number;
  required_coverage_pct?: number | null;
  missing_one_total?: number | null;
  high_cardinality?: number;
}

// --- Change Explorer types ---
export interface ChangeWorkload {
  id: string;
  name: string;
  demo: boolean;
  connection_id: string;
}
export interface ChangeEventDetail {
  detailId: string;
  changeId: string;
  propertyPath: string;
  beforeValue: unknown;
  afterValue: unknown;
  changeType: string;
  technicalSummary: string;
}
export interface ChangeRiskFactor { label: string; delta: number }
export interface ChangeEvent {
  changeId: string;
  runId: string;
  tenantId: string;
  subscriptionId: string;
  workloadId: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  resourceGroup: string;
  location: string;
  eventTime: string;
  operation: string;
  category: string;
  riskScore: number;
  riskLabel: string;
  actor: string;
  actorType: string;
  source: string;
  correlationId: string;
  plainEnglishSummary: string;
  possibleImpact: string;
  confidence: string;
  rawEventJson: Record<string, unknown> | null;
  _hasRaw?: boolean;
  riskFactors: ChangeRiskFactor[];
  dependencyRole: string;
  blastRadius: string;
  whyRisk: string;
  details: ChangeEventDetail[];
  // Identity attribution (resolved post-collect; empty on older cached runs).
  actorDisplay?: string;
  actorObjectId?: string;
  actorKind?: string;
  actorIp?: string;
  actorOnBehalfOf?: string;
  actorResolved?: boolean;
  // Security intelligence (C1/C3).
  securityFlags?: { code: string; label: string; severity: string }[];
  securitySeverity?: string;
  rollbackHint?: string;
}
export interface ChangeOperation {
  operationId: string;
  correlationId: string;
  actor: string;
  actorKind: string;
  verb: string;
  startTime: string;
  endTime: string;
  changeCount: number;
  resourceCount: number;
  categories: string[];
  highestRiskScore: number;
  highestRiskLabel: string;
  securityFlagCount: number;
  resourceNames: string[];
  changeIds: string[];
}
export interface ChangeNarrativeBeat {
  time: string;
  actor: string;
  riskLabel: string;
  riskScore: number;
  securityFlagCount: number;
  text: string;
  changeIds: string[];
  categories: string[];
}
export interface ChangeSecuritySummary {
  flagged_changes: number;
  by_code: Record<string, number>;
  by_severity: Record<string, number>;
}
export interface ChangeCaseFile {
  pinned: string[];
  notes: Record<string, string>;
  caseSummary: string;
  updatedAt?: string;
}
export interface ChangeCompareResult {
  a: { total: number; critical: number; high: number; medium: number; low: number; window: string; runId: string };
  b: { total: number; critical: number; high: number; medium: number; low: number; window: string; runId: string };
  added: ChangeResourceRollup[];
  removed: ChangeResourceRollup[];
  changed: (ChangeResourceRollup & { changesA: number; changesB: number; riskA: number; riskB: number; riskLabelA: string; riskLabelB: string; riskDelta: number })[];
  summary: { added: number; removed: number; changed: number; total_delta: number; critical_delta: number; high_delta: number };
}
export interface ChangeInsight {
  insightId: string;
  runId: string;
  insightType: string;
  title: string;
  summary: string;
  severity: string;
  relatedChangeIds: string[];
}
export interface ChangeResourceRollup {
  resourceId: string;
  resourceName: string;
  resourceType: string;
  resourceGroup: string;
  subscriptionId: string;
  changes: number;
  highestRiskScore: number;
  highestRiskLabel: string;
  lastChanged: string;
  lastActor: string;
  role: string;
}
export interface ChangeActorRollup {
  actor: string;
  actorType: string;
  changes: number;
  highestRiskScore: number;
  highestRiskLabel: string;
  categories: string[];
  resources: number;
  firstChange: string;
  lastChange: string;
  // Identity attribution extensions.
  actorId?: string;
  actorResolved?: boolean;
  ips?: string[];
  onBehalfOf?: string[];
}
export interface ChangeHeadline {
  total: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
  informational: number;
  resources_changed: number;
  unique_actors: number;
  most_active_actor: string;
  most_active_actor_changes: number;
  most_changed_resource_type: string;
  most_risky_category: string;
}
export interface ChangeAnalysisRun {
  runId: string;
  tenantId: string;
  workloadId: string;
  workloadName: string;
  startTime: string;
  endTime: string;
  scopeMode: string;
  requestedBy: string;
  createdAt: string;
  completedAt: string;
  status: string;
  totalChanges: number;
  criticalCount: number;
  highCount: number;
  mediumCount: number;
  lowCount: number;
  informationalCount: number;
  summary: string;
  demo: boolean;
  truncated: boolean;
  changeLimit?: number;
  aiAnalyzed?: boolean;
  notes: string[];
  scopeInfo: Record<string, unknown>;
  facets: { risks: string[]; categories: string[]; actors: string[]; resource_types: string[] };
  events: ChangeEvent[];
  insights: ChangeInsight[];
  headline: ChangeHeadline;
  resources: ChangeResourceRollup[];
  actors: ChangeActorRollup[];
  // Forensics extensions (empty on older cached runs).
  operations?: ChangeOperation[];
  narrative?: ChangeNarrativeBeat[];
  security?: ChangeSecuritySummary;
  caseFile?: ChangeCaseFile;
}
export interface ChangeRunSummary {
  runId: string;
  workloadId: string;
  workloadName: string;
  startTime: string;
  endTime: string;
  scopeMode: string;
  requestedBy: string;
  completedAt: string;
  status: string;
  totalChanges: number;
  criticalCount: number;
  highCount: number;
  mediumCount: number;
  lowCount: number;
  informationalCount: number;
  demo: boolean;
  deleted_at?: string;
}

// ---- Change Explorer Fleet (latest run per workload + mass launch) ---------------
export interface ChangeFleetRow {
  workload_id: string;
  name: string;
  connection_id: string;
  environment: string;
  has_runs: boolean;
  run_id: string;
  run_at: string;
  start_time: string;
  end_time: string;
  scope_mode: string;
  status: string;
  total_changes: number;
  critical_count: number;
  high_count: number;
  medium_count: number;
  low_count: number;
  informational_count: number;
  demo: boolean;
  age_seconds: number | null;
}
export interface ChangeFleet {
  workloads: ChangeFleetRow[];
  total: number;
  analyzed: number;
}

// NL change search ("Ask AI"): the parsed filter spec + which loaded events it matched. When
// the requested time window isn't covered by the loaded run, `in_window` is false and
// `suggested_window` carries the window to re-scan.
export interface ChangeQuerySpec {
  explanation?: string;
  resource_types?: string[];
  categories?: string[];
  actors?: string[];
  actor_types?: string[];
  operations?: string[];
  risk_min?: string;
  name_contains?: string;
  keyword?: string;
  time_window?: { start_iso: string; end_iso: string; label: string } | null;
}
export interface ChangeAskResponse {
  available: boolean;
  answer?: string;
  spec?: ChangeQuerySpec;
  matched_ids?: string[];
  match_count?: number;
  in_window?: boolean;
  suggested_window?: { start_iso: string; end_iso: string; label: string } | null;
  run_id?: string;
  explanation?: string;
}


// --- Inventory types ---
export interface InventoryResource {
  id: string;
  name: string;
  type: string;
  kind: string;
  location: string;
  resource_group: string;
  subscription_id: string;
  tags: Record<string, string>;
  tag_count: number;
  sku: string;
  tier: string;
  size: string;
  managed_by: string;
  flags: string[];
  workloads: { id: string; name: string }[];
}

export interface InventoryFacetItem {
  key: string;
  count: number;
}
export interface InventorySubscriptionFacet extends InventoryFacetItem {
  name: string;
}
export interface InventoryWorkloadFacet {
  id: string;
  name: string;
  count: number;
}

export interface InventoryFacets {
  types: InventoryFacetItem[];
  locations: InventoryFacetItem[];
  subscriptions: InventorySubscriptionFacet[];
  resource_groups: InventoryFacetItem[];
  workloads: InventoryWorkloadFacet[];
  unassigned_count: number;
}

export interface InventorySummary {
  total_resources: number;
  type_count: number;
  subscription_count: number;
  resource_group_count: number;
  location_count: number;
  workload_count: number;
  unassigned_count: number;
  truncated_subscriptions: string[];
  tagged_count: number;
  tag_coverage_pct: number;
  top_tag_keys: InventoryFacetItem[];
  flag_counts: Record<string, number>;
}

export interface InventoryInsight {
  title: string;
  detail: string;
  severity: "info" | "warning" | "critical";
  action: string;
}
export interface InventoryInsights {
  headline: string;
  insights: InventoryInsight[];
  source: "ai" | "local";
}

export interface InventorySnapshot {
  id: string;
  tenant_id: string;
  connection_id: string;
  created_at: string;
  created_by: string;
  total_resources: number;
  type_count: number;
  subscription_count: number;
  tag_coverage_pct: number;
}

export interface InventoryDriftItem {
  id: string;
  name?: string;
  type?: string;
  rg?: string;
  sub?: string;
  sku?: string;
  changes?: Record<string, { from: string; to: string }>;
}
export interface InventoryDrift {
  baseline_id: string;
  baseline_at: string;
  counts: { added: number; removed: number; changed: number };
  added: InventoryDriftItem[];
  removed: InventoryDriftItem[];
  changed: InventoryDriftItem[];
  computed_at: string;
}

export interface InventoryGovernanceItem {
  id: string;
  display_name: string;
  effect: string;
  scope_label: string;
  is_inherited: boolean;
  inherited_from: string;
}
export interface InventoryGovernance {
  scope: string;
  scope_label: string;
  scope_kind: string;
  effective: InventoryGovernanceItem[];
  count: number;
}

export interface InventoryFinding {
  run_id: string;
  workload_id: string;
  workload_name: string;
  check_id: string;
  title: string;
  pillar: string;
  severity: string;
  status: string;
  ai_rationale: string;
  remediation: string;
  started_at: string | null;
}

export interface InventoryCost {
  available: boolean;
  not_loaded?: boolean;
  currency: string;
  period: string;
  fetched_at?: string;
  by_resource: Record<string, number>;
  by_subscription: Record<string, number>;
  total: number;
  errors: string[];
  cached: boolean;
}

export interface InventoryCostBucket {
  key: string;
  name?: string;
  cost: number;
  pct: number;
}

export interface InventoryCostWorkload {
  id: string;
  name: string;
  cost: number;
  pct: number;
  resource_count: number;
}

export interface InventoryCostTopResource {
  id: string;
  name: string;
  type: string;
  location: string;
  subscription_id: string;
  resource_group: string;
  workloads: string[];
  cost: number;
}

export interface InventoryCostRollup {
  available: boolean;
  not_loaded?: boolean;
  currency: string;
  period: string;
  fetched_at: string;
  cached: boolean;
  total: number;
  attributed_total: number;
  unattributed_total: number;
  unassigned_cost: number;
  by_workload: InventoryCostWorkload[];
  by_type: InventoryCostBucket[];
  by_location: InventoryCostBucket[];
  by_subscription: InventoryCostBucket[];
  by_resource_group: InventoryCostBucket[];
  top_resources: InventoryCostTopResource[];
  errors: string[];
}

export interface OptimizationItem {
  id: string;
  name: string;
  type: string;
  location: string;
  resource_group: string;
  subscription_id: string;
  flags: string[];
  category: string;
  category_label: string;
  reason: string;
  remediation: string;
  severity: string;
  monthly_cost: number;
  workloads: { id: string; name: string }[];
}

export interface OptimizationCategory {
  flag: string;
  label: string;
  reason: string;
  remediation: string;
  severity: string;
  count: number;
  monthly_cost: number;
}

export interface InventoryOptimization {
  available: boolean;
  inventory_fetched_at?: string;
  categories: OptimizationCategory[];
  items: OptimizationItem[];
  total_count: number;
  total_monthly_cost?: number;
  currency?: string;
  cost_available?: boolean;
  cost_period?: string;
}

export interface InventoryResponse {
  resources: InventoryResource[];
  facets: InventoryFacets;
  summary: InventorySummary;
  errors: string[];
  cached: boolean;
  fetched_at: string;
  age_seconds: number;
  never_loaded?: boolean;
  // IP6 — set when the server capped the resource rows (pathologically large estate). Facets/
  // summary still reflect the full estate; total_resources_full is the true row count.
  truncated_total?: boolean;
  returned?: number;
  total_resources_full?: number;
}

// The AI-parsed filter from a natural-language query.
export interface InventoryFilter {
  types?: string[];
  locations?: string[];
  subscriptions?: string[];
  resource_groups?: string[];
  workloads?: string[];
  tag_key?: string;
  tag_value?: string;
  sku_contains?: string[];
  text?: string;
}

export interface InventoryNlResult {
  mode: "filter" | "kql";
  filter?: InventoryFilter;
  kql?: string;
  matched_ids?: string[];
  match_count?: number;
  explanation: string;
  error?: string;
}


// --- Azure Policy types ---
export interface PolicyDefinition {
  id: string;
  name: string;
  display_name: string;
  policy_type: string;
  mode: string;
  category: string;
  version: string;
  effect: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface PolicyInitiative {
  id: string;
  name: string;
  display_name: string;
  policy_type: string;
  category: string;
  description: string;
  policy_count: number;
}

export interface PolicyAssignment {
  id: string;
  name: string;
  display_name: string;
  scope: string;
  scope_kind: string;
  scope_label: string;
  policy_definition_id: string;
  definition_name: string;
  is_initiative: boolean;
  enforcement_mode: string;
  effect: string;
  category: string;
  description: string;
  not_scopes: string[];
  identity_type: string;
  identity_principal_id: string;
  location: string;
  parameters: Record<string, unknown>;
  // Governance/audit attribution (from properties.metadata). Optional for back-compat with older
  // cached inventories; populated after a forced refresh. Powers the assignment register + pivots.
  assigned_by?: string;
  created_on?: string;
  created_by?: string;
  updated_on?: string;
  updated_by?: string;
  management_group_name?: string;
  management_group_display?: string;
  subscription_id?: string;
  subscription_name?: string;
}

export interface PolicyExemption {
  id: string;
  name: string;
  display_name: string;
  scope: string;
  scope_kind: string;
  scope_label: string;
  category: string;
  expires_on: string;
  policy_assignment_id: string;
  description: string;
  reference_ids: string[];
  // Enriched by the collector (optional for back-compat with older cached inventories).
  management_group_name?: string;
  management_group_display?: string;
  subscription_id?: string;
  subscription_name?: string;
  status?: string;        // expired | expiring_soon | active | never
  days_left?: number | null;
  assignment_name?: string;
  assignment_is_initiative?: boolean;
  created_on?: string;
  created_by?: string;
  updated_on?: string;
  updated_by?: string;
}

export interface PolicyExemptionGuardrails {
  require_justification: boolean;
  max_expiry_days: number;
  block_never_expires: boolean;
}

export interface PolicyXlsxSheet {
  name: string;
  columns: string[];
  rows: (string | number)[][];
  outline_levels?: number[];
}

export interface PolicyExemptionPayload {
  id?: string;
  name?: string;
  scope?: string;
  policy_assignment_id?: string;
  category?: string;
  display_name?: string;
  description?: string;
  expires_on?: string;
  reference_ids?: string[];
}

export interface PolicyExemptionPlan {
  action: string;
  valid: boolean;
  errors: string[];
  arm: { method: string; path: string; api_version: string; body?: Record<string, unknown> };
  cli: string;
  name: string;
  scope: string;
  guardrails: PolicyExemptionGuardrails;
}

export interface PolicyScopeNode {
  scope: string;
  kind: string;
  label: string;
  depth: number;
  assignments: number;
  exemptions: number;
}

export interface PromoteCandidate {
  assignment_id: string;
  display_name: string;
  scope_label: string;
  current_effect: string;
  non_compliant_resources: number;
  compliance_unknown: boolean;
  safe_to_promote: boolean;
  reason: string;
}

export interface ExemptionHygieneItem {
  id: string;
  display_name: string;
  scope_label: string;
  category: string;
  expires_on: string;
  description: string;
  flags: string[];
  status: string;
}

export interface RemediationGap {
  assignment_id: string;
  display_name: string;
  scope_label: string;
  effect: string;
  is_initiative: boolean;
  issue: string;
  fix: string;
}

export interface PolicyConflict {
  policy_definition_id: string;
  definition_name: string;
  is_initiative?: boolean;
  category?: string;
  assignment_count: number;
  scope_count?: number;
  scopes: {
    id: string;
    scope?: string;
    scope_kind?: string;
    label: string;
    assignment_name?: string;
    effect: string;
    enforcement_mode?: string;
  }[];
  kind: string;
  hint: string;
}

export interface PolicyCompliance {
  by_assignment: Record<string, { non_compliant_resources: number; non_compliant_policies: number }>;
  subscriptions_scanned: number;
  total_non_compliant_resources: number;
  available: boolean;
  errors: string[];
}

export interface PolicyInventory {
  connection_id: string;
  definitions: PolicyDefinition[];
  initiatives: PolicyInitiative[];
  assignments: PolicyAssignment[];
  exemptions: PolicyExemption[];
  errors: string[];
  counts: {
    definitions: number;
    custom_definitions: number;
    initiatives: number;
    assignments: number;
    exemptions: number;
  };
  scope_tree: PolicyScopeNode[];
  // Subscription id (lowercase) → display name, for resolving readable scope labels.
  subscription_names?: Record<string, string>;
  compliance: PolicyCompliance;
  advisors: {
    promote_to_deny: PromoteCandidate[];
    exemption_hygiene: { items: ExemptionHygieneItem[]; buckets: Record<string, number>; total: number };
    remediation_gaps: RemediationGap[];
    conflicts: PolicyConflict[];
  };
  // Present when scoped to an Azure Workload (governance footprint of that workload).
  workload?: PolicyWorkloadScope | null;
  // Server-cache metadata (present on responses; the inventory is cached server-side).
  cached?: boolean;
  fetched_at?: string;
  age_seconds?: number;
  // True for the empty payload returned on a first visit (cache miss, no scan) so the UI
  // prompts the user to press Refresh instead of auto-scanning Azure.
  never_loaded?: boolean;
}

export interface PolicyWorkloadScope {
  id: string;
  name: string;
  subscription_count: number;
  resource_group_count: number;
  resource_count: number;
  ancestor_management_groups: string[];
  scope_ids: string[];
  error: string;
}

export interface PolicyEffectiveItem extends PolicyAssignment {
  inherited_from: string;
  is_inherited: boolean;
  exemptions: PolicyExemption[];
}

export interface PolicyEffective {
  scope: string;
  scope_label: string;
  scope_kind: string;
  effective: PolicyEffectiveItem[];
  count: number;
}

export interface PolicyWhatIf {
  supported: boolean;
  predicate: string;
  count: number;
  sample: { id: string; name: string; type: string; resourceGroup: string; subscriptionId: string; location: string }[];
  blast: {
    risk_score: number;
    risk_level: string;
    summary: string;
    teams_or_rgs_impacted: string[];
    recommendation: string;
  } | null;
  message: string;
}

export interface PolicyAuthorResult {
  display_name: string;
  description: string;
  mode: string;
  recommended_effect: string;
  policy_definition: Record<string, unknown>;
  aliases_used: string[];
  notes: string;
}

export interface PolicyTriageResult {
  likely_policy: string;
  blocked_property: string;
  explanation: string;
  options: { action: string; summary: string; risk: string; steps: string }[];
}

export interface PolicyBaseline {
  id: string;
  label: string;
  description: string;
  control_count: number;
}

export interface PolicyCoverageControl {
  id: string;
  title: string;
  domain: string;
  effect: string;
}

export interface PolicyCoverage {
  baseline_id: string;
  baseline_label: string;
  total: number;
  covered: PolicyCoverageControl[];
  missing: PolicyCoverageControl[];
  covered_count: number;
  missing_count: number;
  coverage_pct: number;
  proposals: {
    control_id: string;
    control_title: string;
    builtin_policy: string;
    effect: string;
    assign_at: string;
    why: string;
  }[];
  // Populated by the server when the analysis is persisted into the history.
  id?: string;
  created_at?: string;
}

// A saved Coverage-gap analysis. The list endpoint returns summaries (no `result`); opening one
// fetches the full record including the original coverage result.
export interface PolicyCoverageRunSummary {
  id: string;
  tenant_id: string;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  baseline_id: string;
  baseline_label: string;
  total: number;
  covered_count: number;
  missing_count: number;
  coverage_pct: number;
  proposals_count: number;
  created_at: string;
  created_by: string;
}

export interface PolicyCoverageRunRecord extends PolicyCoverageRunSummary {
  result: PolicyCoverage;
}

export interface PolicyRolloutPlan {
  summary: string;
  stages: { name: string; enforcement_mode: string; effect: string; selectors: string; exit_criteria: string }[];
  recommended_exemptions: { scope: string; reason: string; expires_in_days: number }[];
  risks: string[];
}

export interface PolicySimulateReq {
  connection_id?: string | null;
  mode: "deploy" | "promote" | "finding";
  intent?: string;
  policy_json?: string;
  assignment_id?: string;
  definition_id?: string;
  current_effect?: string;
  current_enforcement?: string;
  display_name?: string;
  non_compliant_resources?: number;
  // finding mode (assessment hand-off):
  check_id?: string;
  detection_predicate?: string;
  known_impact_count?: number;
  known_sample?: { id: string; name: string; type: string; resourceGroup: string }[];
  frameworks?: { cis?: string[]; nist?: string[]; iso?: string[] };
  remediation?: string;
  resource_types?: string[];
  workload_id?: string;
  title?: string;
  scope: string;
  target_effect: string;
  target_enforcement: string;
}

export interface PolicySimulateResult {
  mode: string;
  display_name: string;
  current_state: { effect: string; enforcement: string };
  target_state: { scope: string; scope_label: string; effect: string; enforcement: string };
  impact: {
    source: string; // none | compliance | resource_graph
    count: number;
    sample: { id: string; name: string; type: string; resourceGroup: string; subscriptionId: string; location: string }[];
    supported: boolean;
    predicate: string;
    message: string;
    affected_resource_groups: string[];
  };
  blast: {
    risk_score: number;
    risk_level: string;
    summary: string;
    teams_or_rgs_impacted: string[];
    recommendation: string;
  } | null;
  plan: {
    summary: string;
    impact_interpretation: string;
    stages: { name: string; enforcement_mode: string; effect: string; selectors: string; exit_criteria: string; duration: string }[];
    prerequisites: string[];
    recommended_exemptions: { scope: string; reason: string; expires_in_days: number }[];
    risks: string[];
    go_no_go: string;
    rationale: string;
  } | null;
  artifacts: {
    assignment_json?: Record<string, unknown>;
    az_commands?: string[];
    policy_definition?: Record<string, unknown>;
    aliases_used?: string[];
  };
  authored: PolicyAuthorResult | null;
  // finding-mode extras
  check_id?: string;
  workload_id?: string;
  frameworks?: { cis?: string[]; nist?: string[]; iso?: string[] };
  builtin_match?: {
    matched: boolean;
    definition_id: string;
    builtin_display_name: string;
    recommended_effect: string;
    confidence: number;
    reasoning: string;
    custom_needed: boolean;
  } | null;
}

// A finding handed over from the assessment report to the Rollout Planner.
export interface PolicyHandoffFinding {
  check_id: string;
  title: string;
  description: string;
  severity: string;
  pillar: string;
  frameworks: { cis?: string[]; nist?: string[]; iso?: string[] };
  remediation: string;
  remediation_command: string;
  resource_types: string[];
  flagged_count: number;
  flagged_resources: { id: string; name: string; type: string; resourceGroup: string }[];
  suggested_effect: string;
}

export interface PolicyHandoff {
  source: "assessment";
  run_id: string;
  workload_id: string;
  connection_id: string | null;
  findings: PolicyHandoffFinding[];
}

// Hand-off from Tag Intelligence → Policy generator into the Rollout Planner: the generated tag
// policy definitions, pre-loaded into the planner's deploy mode as ready-to-simulate context.
export interface PolicyTagHandoffDefinition {
  tag: string;
  effect: string;        // audit | deny | modify
  name: string;
  displayName: string;
  json: string;          // the policy definition `properties` JSON (pretty-printed)
}
export interface PolicyTagHandoff {
  source: "tagintel";
  scope?: string;
  definitions: PolicyTagHandoffDefinition[];
}

// A saved Safe-Rollout simulation. The list endpoint returns summaries (no `result`); opening
// one fetches the full record including the original simulation result.
export interface PolicySimulationSummary {
  id: string;
  tenant_id: string;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  mode: string;
  title: string;
  scope: string;
  scope_label: string;
  target_effect: string;
  target_enforcement: string;
  impact_count: number;
  impact_supported: boolean;
  risk_level: string;
  go_no_go: string;
  check_id: string;
  created_at: string;
  created_by: string;
}

export interface PolicySimulationRecord extends PolicySimulationSummary {
  result: PolicySimulateResult;
}

export interface PolicyEnforcementLink {
  tenant_id: string;
  workload_id: string;
  check_id: string;
  title: string;
  definition_id: string;
  builtin_name: string;
  target_effect: string;
  target_scope: string;
  go_no_go: string;
  plan_summary: string;
  impact_count: number;
  frameworks: { cis?: string[]; nist?: string[]; iso?: string[] };
  planned_by: string;
  planned_at: string;
}

export interface PolicySimStatus {
  key: string;
  message: string;
  detail: string;
}

/** Stream the AI Safe-Rollout Planner simulation over SSE for live progress. */
export async function streamPolicySimulate(
  body: PolicySimulateReq,
  handlers: {
    onStatus?: (s: PolicySimStatus) => void;
    onDone?: (r: PolicySimulateResult) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/policy/simulate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as unknown as PolicySimStatus);
      else if (event === "done") handlers.onDone?.(parsed as unknown as PolicySimulateResult);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Simulation failed.");
    }
  }
}

export async function streamNetcheckRun(
  body: NetCheckRunRequest,
  handlers: {
    onStart?: (d: { source: string; target: string; port: number; protocol: string }) => void;
    onStep?: (s: NetCheckStep) => void;
    onEvidence?: (e: NetCheckEvidence) => void;
    onDone?: (d: { run: NetCheckRun; diff: NetCheckDiff[]; previous_id: string }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/netcheck/run/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "step") handlers.onStep?.(parsed as unknown as NetCheckStep);
      else if (event === "evidence") handlers.onEvidence?.(parsed as unknown as NetCheckEvidence);
      else if (event === "done") handlers.onDone?.(parsed as never);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Run failed.");
    }
  }
}

export async function streamDnsDebug(
  body: DnsDebugRunRequest,
  handlers: {
    onStart?: (d: { fqdn: string; source_count: number }) => void;
    onEvidence?: (e: DnsZoneFacts) => void;
    onSourceStart?: (d: { source: string; vm_id: string }) => void;
    onStep?: (s: DnsStep) => void;
    onSourceDone?: (s: DnsSourceResult) => void;
    onDone?: (d: { run: DnsDebugRun; diff: DnsDebugDiff[]; previous_id: string }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/dnsdebug/run/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "evidence") handlers.onEvidence?.(parsed as unknown as DnsZoneFacts);
      else if (event === "source_start") handlers.onSourceStart?.(parsed as never);
      else if (event === "step") handlers.onStep?.(parsed as unknown as DnsStep);
      else if (event === "source_done") handlers.onSourceDone?.(parsed as unknown as DnsSourceResult);
      else if (event === "done") handlers.onDone?.(parsed as never);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Run failed.");
    }
  }
}

export async function streamTeleintelAsk(
  body: { question: string; workload_id?: string; subscription_id?: string; connection_id?: string; component_id?: string },
  handlers: {
    onStart?: (d: { question: string }) => void;
    onKql?: (d: { kql: string; explanation: string }) => void;
    onRows?: (d: { rows: Record<string, unknown>[]; path: string }) => void;
    onAnswer?: (d: { answer: string }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/teleintel/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "kql") handlers.onKql?.(parsed as never);
      else if (event === "rows") handlers.onRows?.(parsed as never);
      else if (event === "answer") handlers.onAnswer?.(parsed as never);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Query failed.");
    }
  }
}

export async function streamPerfRefresh(
  params: { workload_id?: string; subscription_id?: string; connection_id?: string; window?: string; start_time?: string; end_time?: string },
  handlers: {
    onStart?: (d: { scope_kind: string; scope_id: string }) => void;
    onProgress?: (d: { resource: string; type: string }) => void;
    onDone?: (d: PerfProfile) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const q = new URLSearchParams();
  if (params.workload_id) q.set("workload_id", params.workload_id);
  if (params.subscription_id) q.set("subscription_id", params.subscription_id);
  if (params.connection_id) q.set("connection_id", params.connection_id);
  if (params.window) q.set("window", params.window);
  if (params.start_time) q.set("start_time", params.start_time);
  if (params.end_time) q.set("end_time", params.end_time);
  const res = await fetch(`${API_BASE}/performance/refresh/stream?${q.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "progress") handlers.onProgress?.(parsed as never);
      else if (event === "done") handlers.onDone?.(parsed as unknown as PerfProfile);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Profiling failed.");
    }
  }
}

// ---- Change Explorer (SSE) -----------------------------------------------------
export interface ChangeAnalyzeBody {
  workload_id?: string; subscription_id?: string; subscription_name?: string;
  connection_id?: string; start_time: string; end_time: string; scope_mode: string;
  run_ai?: boolean;
}
export interface ChangeProgress { phase: string; message: string; done?: number; total?: number }
/** Live change analysis over SSE: start → progress* (collecting / classifying / AI analyzing) →
 *  done(run). The server persists the run before 'done', so a dropped stream is recoverable from
 *  history. */
export async function streamChangeExplorerAnalyze(
  body: ChangeAnalyzeBody,
  handlers: {
    onStart?: (d: { workloadName: string; scopeMode: string }) => void;
    onProgress?: (d: ChangeProgress) => void;
    onDone?: (run: ChangeAnalysisRun) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  return _streamChangeExplorerSse(`${API_BASE}/changeexplorer/analyze/stream`, JSON.stringify(body), handlers, signal);
}

/** Run the AI enrichment pass over an already-analyzed run (when the 'Perform AI analysis'
 *  checkbox was off). Streams progress → done(updated run); the server persists before 'done'. */
export async function streamChangeExplorerAiEnrich(
  runId: string,
  handlers: {
    onProgress?: (d: ChangeProgress) => void;
    onDone?: (run: ChangeAnalysisRun) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  return _streamChangeExplorerSse(`${API_BASE}/changeexplorer/runs/${encodeURIComponent(runId)}/ai-enrich/stream`, "{}", handlers, signal);
}

async function _streamChangeExplorerSse(
  url: string,
  bodyJson: string,
  handlers: {
    onStart?: (d: { workloadName: string; scopeMode: string }) => void;
    onProgress?: (d: ChangeProgress) => void;
    onDone?: (run: ChangeAnalysisRun) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: bodyJson,
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b?.detail) detail = b.detail; } catch { /* ignore */ }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try { parsed = JSON.parse(data); } catch { continue; }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "progress") handlers.onProgress?.(parsed as unknown as ChangeProgress);
      else if (event === "done") handlers.onDone?.(parsed as unknown as ChangeAnalysisRun);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Analysis failed.");
    }
  }
}

// ---- Tag Intelligence — apply remediation (SSE) --------------------------------
export interface TagApplyStart { total: number; connection?: string; subscription_count?: number }
export interface TagApplyItemStart {
  index: number; total: number; id: string; name: string; type?: string; resource_group?: string; change: string;
}
export interface TagApplyItemDone {
  index: number; total: number; id: string; name: string; ok: boolean; error: string; applied: number; failed: number;
}
/** Apply a tag change-set to Azure with a live per-resource status feed over SSE:
 *  start → (item_start → item_done)* → done(TagApplyResult). The server persists the audit
 *  trail before 'done', and governance (read-only connection / approval) surfaces as a blocked
 *  'done'. */
export async function streamTagintelRemediateApply(
  sel: TagScopeSel,
  operations: TagRemediationOp[],
  changeset_id: string | undefined,
  handlers: {
    onStart?: (d: TagApplyStart) => void;
    onItemStart?: (d: TagApplyItemStart) => void;
    onItemDone?: (d: TagApplyItemDone) => void;
    onDone?: (r: TagApplyResult) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/tagintel/remediate/apply/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tagBody({ ...sel, operations, approved: true, changeset_id: changeset_id || "" })),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b?.detail) detail = b.detail; } catch { /* ignore */ }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try { parsed = JSON.parse(data); } catch { continue; }
      if (event === "start") handlers.onStart?.(parsed as unknown as TagApplyStart);
      else if (event === "item_start") handlers.onItemStart?.(parsed as unknown as TagApplyItemStart);
      else if (event === "item_done") handlers.onItemDone?.(parsed as unknown as TagApplyItemDone);
      else if (event === "done") handlers.onDone?.(parsed as unknown as TagApplyResult);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Apply failed.");
    }
  }
}

// ---- Workload Mission Control (SSE) --------------------------------------------
/** Follow a mission's live progress over SSE: snapshot → per-system + log deltas → done.
 *  The mission keeps running server-side even if this stream disconnects. */
export async function streamMission(
  missionId: string,
  handlers: {
    onSnapshot?: (m: Mission) => void;
    onSystem?: (s: MissionSystem) => void;
    onLog?: (d: MissionLog) => void;
    onDone?: (m: Mission) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/missions/${encodeURIComponent(missionId)}/stream`, {
    method: "GET",
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "snapshot") handlers.onSnapshot?.(parsed as unknown as Mission);
      else if (event === "system") handlers.onSystem?.(parsed as unknown as MissionSystem);
      else if (event === "log") handlers.onLog?.(parsed as unknown as MissionLog);
      else if (event === "done") handlers.onDone?.(parsed as unknown as Mission);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Mission failed.");
    }
  }
}

// ---- RBAC per-scope refresh (SSE) ----------------------------------------------
/** Follow a per-scope RBAC refresh over SSE. The server job keeps running even if this stream
 *  disconnects — re-calling re-attaches and replays the log. mode: scope | directory | all. */
export async function streamRbacRefresh(
  params: { scope?: string; mode?: string; display_name?: string; connection_id?: string | null },
  handlers: {
    onStart?: (d: RbacJob) => void;
    onProgress?: (d: RbacProgress) => void;
    onDone?: (d: { key: string; scope: string; mode: string }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const q = new URLSearchParams();
  if (params.scope) q.set("scope", params.scope);
  if (params.mode) q.set("mode", params.mode);
  if (params.display_name) q.set("display_name", params.display_name);
  if (params.connection_id) q.set("connection_id", params.connection_id);
  const res = await fetch(`${API_BASE}/rbac/refresh/stream?${q.toString()}`, {
    method: "GET",
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "progress") handlers.onProgress?.(parsed as never);
      else if (event === "done") handlers.onDone?.(parsed as never);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Refresh failed.");
    }
  }
}

// ---- App Registrations background refresh (SSE) --------------------------------
export type AppRegProgress = { seq: number; ts: string; level: "info" | "ok" | "warn" | "error"; message: string };

/** Follow the background Application Registrations refresh over SSE. The server job keeps
 *  running even if this stream disconnects — re-calling re-attaches and replays the log. */
export async function streamAppRegistrationsRefresh(
  handlers: {
    onStart?: (d: { id: string; status: string; started_at: string }) => void;
    onProgress?: (d: AppRegProgress) => void;
    onDone?: (d: AppRegistrationsResponse) => void;
    onError?: (msg: string) => void;
  },
  connectionId?: string | null,
  signal?: AbortSignal,
): Promise<void> {
  const qs = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
  const res = await fetch(`${API_BASE}/identity/app-registrations/refresh/stream${qs}`, {
    method: "GET",
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "start") handlers.onStart?.(parsed as never);
      else if (event === "progress") handlers.onProgress?.(parsed as unknown as AppRegProgress);
      else if (event === "done") handlers.onDone?.(parsed as unknown as AppRegistrationsResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Refresh failed.");
      // "ping" heartbeats are ignored.
    }
  }
}

export interface LlmTestStep {
  step:
    | "config"
    | "endpoint"
    | "connect"
    | "auth"
    | "request"
    | "first_token"
    | "complete"
    | "fetch";
  status: "ok" | "error" | "warn" | "skip";
  title: string;
  detail: string;
  ms?: number;
}

/** Stream staged LLM-provider connection diagnostics over SSE. Emits one
 *  `step` event per phase (config → endpoint → connect → auth → request →
 *  first_token → complete) so the admin can see exactly where it fails. */
export async function streamTestLlmProvider(
  provider: string,
  handlers: {
    onStep?: (s: LlmTestStep) => void;
    onDone?: (r: { ok: boolean; detail: string }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/admin/llm/test/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider }),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "step") handlers.onStep?.(parsed as unknown as LlmTestStep);
      else if (event === "done")
        handlers.onDone?.(parsed as unknown as { ok: boolean; detail: string });
      else if (event === "error")
        handlers.onError?.((parsed.message as string) ?? "Test failed.");
    }
  }
}

/** Stream staged diagnostics for the "Refresh models" button (config → endpoint →
 *  connect → fetch → complete) plus a final `models` list to update the dropdown. */
export async function streamRefreshLlmModels(
  provider: string,
  freeOnly: boolean | undefined,
  handlers: {
    onStep?: (s: LlmTestStep) => void;
    onDone?: (r: { ok: boolean; detail: string; models: string[] }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const body: { provider: string; free_only?: boolean } = { provider };
  if (freeOnly !== undefined) body.free_only = freeOnly;
  const res = await fetch(`${API_BASE}/admin/llm/models/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "step") handlers.onStep?.(parsed as unknown as LlmTestStep);
      else if (event === "done")
        handlers.onDone?.(
          parsed as unknown as { ok: boolean; detail: string; models: string[] },
        );
      else if (event === "error")
        handlers.onError?.((parsed.message as string) ?? "Refresh failed.");
    }
  }
}

export interface PolicyTagGovernance {
  required_tags: string[];
  missing_count: number;
  sample: { id: string; name: string; type: string; resourceGroup: string }[];
  error: string;
  proposal: {
    summary?: string;
    modify_policies?: { tag: string; approach: string; policy_hint: string }[];
    audit_policy?: string;
    notes?: string;
  };
}

export interface PolicyIacSource {
  content: string;
  format: string;
  updated_at: string;
}

export interface PolicyDriftResult {
  in_sync: boolean;
  live_only: { name: string; scope: string; note: string }[];
  code_only: { name: string; note: string }[];
  mismatched: { name: string; difference: string }[];
  recommendation: string;
}

export interface PolicyDrift {
  assignments_delta: number;
  exemptions_delta: number;
  definitions_delta: number;
  non_compliant_delta: number;
}

export interface PolicySnapshot {
  id: string;
  tenant_id: string;
  connection_id: string;
  created_at: string;
  created_by: string;
  summary: {
    counts: Record<string, number>;
    by_effect: Record<string, number>;
    by_enforcement: Record<string, number>;
    compliance: { available: boolean; total_non_compliant_resources: number; subscriptions_scanned: number };
  };
}

export interface PolicyDraft {
  id: string;
  tenant_id: string;
  title: string;
  kind: string;
  intent: string;
  policy_json: Record<string, unknown>;
  notes: string;
  created_at: string;
  created_by: string;
  updated_at: string;
  updated_by: string;
}


// --- Access-control types ---
export interface AcUser {
  id: string;
  username: string;
  email: string;
  display_name: string;
  status: string;
  auth_source: string;
  must_change_password: boolean;
  tenant_id: string;
  role_ids: string[];
  group_ids: string[];
  role_names: string[];
  permissions: string[];
  last_login_at: string | null;
  locked: boolean;
  created_at: string | null;
}

export interface AcUserCreate {
  username: string;
  email: string;
  display_name?: string;
  password?: string | null;
  role_ids?: string[];
  group_ids?: string[];
  must_change_password?: boolean;
}

export interface AcUserUpdate {
  email?: string;
  display_name?: string;
  status?: string;
  role_ids?: string[];
  group_ids?: string[];
}

export interface AcRole {
  id: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

export interface AcRoleBody {
  name: string;
  description?: string;
  permissions: string[];
}

export interface AcGroup {
  id: string;
  name: string;
  description: string;
  role_ids: string[];
  member_count?: number;
}

export interface AcGroupBody {
  name: string;
  description?: string;
  role_ids: string[];
}

export interface AcIdp {
  id: string;
  name: string;
  type: string;
  enabled: boolean;
  button_label: string;
  config: Record<string, unknown>;
}

export interface AcIdpBody {
  name: string;
  type: string;
  enabled: boolean;
  button_label?: string;
  config: Record<string, unknown>;
}

export interface IdpTestCheck {
  name: string;
  ok: boolean;
  detail: string;
  critical: boolean;
}

export interface IdpTestResult {
  ok: boolean;
  checks: IdpTestCheck[];
  summary: string;
}

export interface AcSession {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
  ip: string | null;
  user_agent: string | null;
  created_at: string | null;
  last_seen_at: string | null;
  expires_at: string | null;
  expired?: boolean;
  status?: "active" | "expired";
}

export interface AuthPolicies {
  local_login_enabled: boolean;
  allow_self_registration: boolean;
  password_min_length: number;
  password_require_complexity: boolean;
  max_failed_attempts: number;
  lockout_minutes: number;
  ip_rate_limit_enabled: boolean;
  ip_rate_limit_max_attempts: number;
  ip_rate_limit_window_seconds: number;
  ip_rate_limit_lockout_seconds: number;
  session_idle_minutes: number;
  session_absolute_minutes: number;
  sso_auto_provision: boolean;
  sso_default_role: string;
}

export interface ConnectorFieldMeta {
  key: string;
  label: string;
  type: string;
  placeholder: string;
  secret: boolean;
  optional: boolean;
  help: string;
  options: string[];
}

export interface ConnectorTypeMeta {
  id: string;
  label: string;
  description: string;
  modes: Record<string, ConnectorFieldMeta[]>;
}

export interface AppConnector {
  id: string;
  name: string;
  type: string;
  mode: string;
  disabled: boolean;
  status: string;
  status_detail: string;
  config: Record<string, string | boolean>;
  created_at: string;
  updated_at: string;
}

export interface ConnectorUpsert {
  id?: string;
  name: string;
  type: string;
  mode: string;
  disabled?: boolean;
  config: Record<string, string>;
}

export interface ConnectorToolInfo {
  name: string;
  description: string;
  kind: string;
  connector_id: string;
  connector_name: string;
  connector_type: string;
}

export interface CustomAgent {
  id: string;
  name: string;
  instructions: string;
  category?: string;
  provider: string;
  model: string;
  connection_id: string;
  allow_all_azure: boolean;
  allow_all_entra?: boolean;
  connector_tools: string[];
  run_mode: string;
  enabled?: boolean;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AgentCategory {
  id: string;
  label: string;
  icon: string;
}

// --- AI agent designer (wizard) ---
export interface AgentWizardQuestion {
  id: string;
  prompt: string;
  kind: "single" | "multi" | "text";
  options: string[];
  allow_custom: boolean;
}

export interface AgentInterviewResult {
  questions: AgentWizardQuestion[];
  done: boolean;
  note: string;
}

export interface AgentAnswer {
  id: string;
  prompt: string;
  answer: string | string[];
}

export interface AgentDraft {
  name: string;
  instructions: string;
  connector_tools: string[];
  allow_all_azure: boolean;
  run_mode: string;
  suggested_provider: string;
  suggested_model: string;
  category?: string;
  summary: string;
  rationale: string;
}

// --- AI Insight Packs ---
export type InsightVerdict = "nothing_notable" | "notable" | "urgent";

export interface InsightPack {
  id: string;
  name: string;
  icon: string;
  category: string;
  description: string;
  sources: string[];
  supported_scopes: string[];
  lookback_hours: number;
  filters: { categories?: string[]; operations?: string[]; min_risk?: string };
  materiality: { notify_threshold: InsightVerdict; always_notify_if: string[] };
  output: { format?: string[]; table_columns?: string[] };
  instructions: string;
  enabled: boolean;
  builtin: boolean;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
  snoozed_until?: string;
  pinned?: boolean;
  collection_ids?: string[];
}

export interface InsightCollection {
  id: string;
  name: string;
  icon?: string;
  created_by?: string;
  created_at?: string;
}

export interface InsightSource {
  id: string;
  label: string;
  icon: string;
  description: string;
}

export interface InsightFlagCode {
  code: string;
  label: string;
}

export interface InsightPackLibrary {
  packs: InsightPack[];
  categories: { id: string; label: string; icon: string }[];
  collections: InsightCollection[];
  sources: InsightSource[];
  flag_codes: InsightFlagCode[];
  verdicts: InsightVerdict[];
}

// --- AI Insight Pack wizard (richer than the shared AgentWizardQuestion) ---
export interface InsightWizardOption {
  value: string;
  description?: string;
  recommended?: boolean;
}

export interface InsightWizardQuestion {
  id: string;
  prompt: string;
  kind: "single" | "multi" | "text";
  options: InsightWizardOption[];
  allow_custom: boolean;
  help?: string;
  required?: boolean;
}

export interface InsightInterviewResult {
  questions: InsightWizardQuestion[];
  done: boolean;
  note: string;
  off_topic?: boolean;
  suggestions?: string[];
}

// Fast, deterministic (no-LLM) best-guess of the pack while the interview is in progress.
export interface InsightPackPreview {
  name?: string;
  category?: string;
  sources?: string[];
  lookback_hours?: number;
  materiality?: { notify_threshold?: InsightVerdict; always_notify_if?: string[] };
  source_labels?: string[];
}

// --- AI copilot for the pack editor (refine) ---
export type InsightRefineMode =
  | "command"
  | "improve_instructions"
  | "suggest"
  | "explain"
  | "critique"
  | "sample";

export interface InsightRefineChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface InsightCritiqueFinding {
  severity: "high" | "medium" | "low";
  message: string;
  field?: string | null;
}

export interface InsightSampleFinding {
  verdict: InsightVerdict;
  headline: string;
  bullets: string[];
  table: { time: string; change: string; risk: string; owner: string; recommended_action: string }[];
}

// A refine call returns exactly one of these shapes depending on the mode requested.
export interface InsightRefineResult {
  pack?: InsightPack;
  changes?: InsightRefineChange[];
  changed_fields?: string[];
  rationale?: string;
  explanation?: string;
  findings?: InsightCritiqueFinding[];
  sample?: InsightSampleFinding;
}

export interface InsightScope {
  mode: "tenant" | "subscription" | "workload" | "workload_dependencies";
  workload_ids?: string[];
  workload_id?: string;
  workload_names?: string[];
  subscription_id?: string;
  subscription_name?: string;
  connection_id?: string;
}

export interface InsightTableRow {
  time: string;
  workload: string;
  change: string;
  risk: string;
  owner: string;
  recommended_action: string;
}

export interface InsightRun {
  id: string;
  pack_id: string;
  pack_name: string;
  pack_icon: string;
  tenant_id: string;
  trigger: string;
  task_id?: string | null;
  scope: InsightScope;
  scope_label: string;
  lookback_hours: number;
  verdict: InsightVerdict;
  headline: string;
  bullets: string[];
  table: InsightTableRow[];
  counts: { changes: number; flags: string[] };
  sources: string[];
  notified: boolean;
  gate_reason: string;
  ai_error?: string | null;
  status: string;
  created_at?: string;
  read_at?: string | null;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  false_positive?: boolean;
}

export interface InsightOccurrence {
  task_id: string;
  task_name: string;
  pack_id: string;
  pack_name: string;
  pack_icon: string;
  at: string;
  schedule_label: string;
  scope_label?: string;
}

export interface InsightRunStep {
  ts: number;
  stage: string;
  label: string;
  detail: string;
  state: "done" | "active" | "error";
}

export interface InsightRunJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  label: string;
  pct: number;
  steps: InsightRunStep[];
  run: InsightRun | null;
  error: string | null;
  pack_name: string;
  scope_label: string;
}

export interface InsightWatcher {  task_id: string;
  task_name: string;
  enabled: boolean;
  pack_id: string;
  pack_name: string;
  pack_icon: string;
  category: string;
  sources: string[];
  lookback_hours: number;
  scope: InsightScope;
  scope_key: string;
  scope_label: string;
  schedule_label: string;
  next_run_at?: string | null;
  status: "covered" | "stale" | "paused";
  last_run_id?: string | null;
  last_verdict?: InsightVerdict | null;
  last_run_at?: string | null;
  last_headline?: string | null;
  last_notified?: boolean;
}

export interface InsightCoverageArea {
  area: string;
  label: string;
  icon: string;
  status: "covered" | "stale" | "paused" | "gap";
  packs: InsightWatcher[];
}

export interface InsightCoverage {
  watchers: InsightWatcher[];
  categories: { id: string; label: string; icon: string }[];
  workload_id?: string;
  workload_name?: string;
  areas?: InsightCoverageArea[];
  gaps?: string[];
  summary?: { covered: number; stale: number; paused: number; gaps: number };
  upcoming?: InsightOccurrence[];
  recent_runs?: InsightRun[];
}

export interface InsightPackHealth {
  pack_id: string;
  pack_name: string;
  pack_icon: string;
  runs_total: number;
  notified: number;
  false_positive: number;
  acknowledged: number;
  verdicts: { nothing_notable: number; notable: number; urgent: number };
  spark: number[];
  last_run_at?: string | null;
  last_verdict?: InsightVerdict | null;
  material: number;
  noise_score: number;
  fp_rate: number;
  suggest_raise_threshold: boolean;
}

// Portable agent config export shapes.
export interface AgentConfig {
  name: string;
  instructions: string;
  provider: string | null;
  model: string | null;
  connection_id: string | null;
  allow_all_azure: boolean;
  allow_all_entra?: boolean;
  connector_tools: string[];
  run_mode: string;
}
export interface AgentExport {
  version: number;
  kind: string;
  agent: AgentConfig;
}
export interface AgentBundleExport {
  version: number;
  kind: string;
  count: number;
  agents: AgentConfig[];
}

// --- AI agent enhancer (existing agent) ---
export interface AgentEnhanceInterview {
  assessment: string;
  questions: AgentWizardQuestion[];
  done: boolean;
  note: string;
}
export interface AgentEnhanceDraft {
  name: string;
  instructions: string;
  connector_tools: string[];
  allow_all_azure: boolean;
  run_mode: string;
  summary: string;
  changes: string[];
}
export interface AgentEnhanceCurrent {
  name: string;
  instructions: string;
  connector_tools: string[];
  run_mode: string;
  allow_all_azure: boolean;
}
export interface AgentEnhanceResult {
  draft: AgentEnhanceDraft;
  current: AgentEnhanceCurrent;
}

export interface ScheduledTask {
  id: string;
  name: string;
  instructions: string;
  agent_id: string | null;
  connection_id: string | null;
  target_type: "agent" | "assessment" | "workbook" | "playbook" | "insight_pack";
  target_config?: Record<string, unknown>;
  target_label?: string;
  target_meta?: { label: string; icon: string };
  schedule_kind: "daily" | "weekly" | "cron";
  cron_expr: string | null;
  time_of_day: string | null;
  weekday: number | null;
  timezone: string;
  start_date: string | null;
  end_date: string | null;
  max_runs: number | null;
  run_mode: string;
  message_grouping: string;
  notify_connector_ids?: string[];
  thread_id: string | null;
  status: string;
  completed_runs: number;
  last_run_at: string | null;
  last_status?: string | null;
  next_run_at: string | null;
  deleted_at?: string | null;
  run_count?: number;
  schedule_label?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface TaskMetrics {
  active: number;
  total: number;
  total_runs: number;
  failed?: number;
}

export interface TaskRunInfo {
  id: string;
  thread_id: string | null;
  trigger: string;
  status: string;
  summary: string | null;
  error: string | null;
  target_type?: string;
  result_ref?: Record<string, unknown> | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
}

export type BackupConflictMode = "skip" | "overwrite" | "merge";

export interface BackupSection {
  id: string;
  label: string;
  tier: "config" | "reference" | "secrets" | "data";
  kind: "collection" | "document" | "db";
  secret_bearing: boolean;
  count: number;
}

export interface BackupManifest {
  format: string;
  version: number;
  exported_at: string;
  meta: { tenant_id: string; sections: string[]; secrets_required: string[] };
  sections: Record<string, unknown>;
}

export interface BackupSectionDiff {
  id: string;
  label: string;
  tier: string;
  kind: string;
  incoming: number;
  create: number;
  update: number;
  skip: number;
  ignored?: boolean;
}

export interface BackupImportPreview {
  mode: BackupConflictMode;
  exported_at: string | null;
  source_tenant: string | null;
  secrets_required: string[];
  sections: BackupSectionDiff[];
}

export interface BackupImportResult {
  mode: BackupConflictMode;
  secrets_required: string[];
  sections: { id: string; created: number; updated: number; skipped: number }[];
}

export interface TenantOption {
  id: string;
  display_name: string;
  tenant_id: string;
  is_default: boolean;
  status: string;
  read_only: boolean;
}

export interface EntraValidation {
  status: string;
  appId?: string;
  displayName?: string;
  servicePrincipalId?: string;
  required: string[];
  granted: string[];
  missing: string[];
  extra: string[];
  satisfied: boolean;
  summary: string;
}

export interface AzureConnection {
  id: string;
  display_name: string;
  tenant_id: string;
  auth_method: string;
  default_subscription: string;
  read_only: boolean;
  auto_execute_writes: boolean;
  disabled: boolean;
  is_default: boolean;
  status: string;
  status_detail: string;
  last_tested: string;
  token_expires_on: string;
  client_id: string;
  has_client_secret: boolean;
  has_certificate: boolean;
  has_access_token: boolean;
  client_secret_hint: string;
  access_token_hint: string;
  has_graph_access_token?: boolean;
  graph_access_token_hint?: string;
  graph_token_expires_on?: string;
  created_at: string;
  updated_at: string;
}

export interface ConnectionUpsert {
  id?: string;
  display_name: string;
  tenant_id: string;
  auth_method: string;
  default_subscription?: string;
  read_only?: boolean;
  auto_execute_writes?: boolean;
  disabled?: boolean;
  is_default?: boolean;
  client_id?: string;
  client_secret?: string;
  certificate_pem?: string;
  access_token?: string;
  access_token_json?: string;
  token_expires_on?: string;
  graph_access_token?: string;
  graph_access_token_json?: string;
  graph_token_expires_on?: string;
}

export interface SandboxVm {
  id: string;
  display_name: string;
  host: string;
  port: number;
  username: string;
  auth_method: string;
  strict_mode: boolean;
  disabled: boolean;
  allow_sudo: boolean;
  workload_ids: string[];
  vnet_label: string;
  os_info: string;
  capabilities: string[];
  pkg_manager: string;
  can_sudo: boolean;
  sudo_mode: string;
  has_private_key: boolean;
  has_password: boolean;
  host_key_fingerprint: string;
  password_hint: string;
  status: string;
  status_detail: string;
  last_tested: string;
  created_at: string;
  updated_at: string;
}

export interface SandboxVmUpsert {
  id?: string;
  display_name: string;
  host: string;
  port: number;
  username: string;
  auth_method: string;
  strict_mode?: boolean;
  disabled?: boolean;
  allow_sudo?: boolean;
  workload_ids?: string[];
  vnet_label?: string;
  ssh_private_key?: string;
  ssh_passphrase?: string;
  ssh_password?: string;
}

export interface SandboxVmRun {
  id: string;
  vm_id: string;
  vm_name: string | null;
  command: string | null;
  destructive: boolean;
  status: string;
  exit_code: number | null;
  output: string;
  stderr: string;
  trigger: string;
  chat_id: string | null;
  triggered_by: string;
  error: string | null;
  duration_ms: number | null;
  created_at: string | null;
}

export interface AuditEntry {
  id: string;
  actor_id: string;
  action: string;
  target: string | null;
  provider: string | null;
  model: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditPage {
  items: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface SiemExportStatus {
  last_success_at: string | null;
  last_attempt_at: string | null;
  last_error: string | null;
  forwarded_total: number;
  cursor_ts: string | null;
  configured: boolean;
}

export interface SiemDestination {
  id: string;
  name: string;
  enabled: boolean;
  type: string;
  endpoint: string;
  token_set: boolean;
  auth_header: string;
  auth_scheme: string;
  splunk_index: string;
  splunk_sourcetype: string;
  verify_tls: boolean;
  batch_size: number;
  status: SiemExportStatus;
}

export interface SiemDestinationsResponse {
  destinations: SiemDestination[];
  types: string[];
}

export interface SiemDestinationInput {
  name: string;
  enabled: boolean;
  type: string;
  endpoint: string;
  token: string;
  clear_token: boolean;
  auth_header: string;
  auth_scheme: string;
  splunk_index: string;
  splunk_sourcetype: string;
  verify_tls: boolean;
  batch_size: number;
}

export interface MonitorOverview {
  generated_at: string;
  window?: { days: number | null; since: string | null };
  totals: {
    chats: number;
    messages: number;
    messages_24h: number;
    tool_calls: number;
    tool_calls_24h: number;
    pending_approvals: number;
    task_runs: number;
    active_schedules: number;
    total_schedules: number;
    custom_agents: number;
    connectors: number;
    connectors_ok: number;
    deep_investigations: number;
    live_turns: number;
  };
  live_turns: {
    chat_id: string;
    title: string;
    user_id: string;
    kind: string;
    elapsed_s: number;
    current_tool: string | null;
    tool_count: number;
    started_at: number | null;
  }[];
  tokens: {
    prompt: number;
    completion: number;
    total: number;
    requests: number;
    cost_usd: number;
    by_model: { model: string; requests: number; prompt: number; completion: number; total: number; cost_usd: number }[];
  };
  tool_calls: {
    by_status: Record<string, number>;
    by_kind: Record<string, number>;
    top_tools: { name: string; count: number }[];
    failed_recent: {
      tool_name: string;
      status: string;
      kind: string;
      chat_id: string;
      created_at: string;
    }[];
    succeeded: number;
    failed: number;
  };
  tool_latency: { name: string; avg_ms: number; count: number }[];
  providers: { provider: string; count: number }[];
  activity_14d: { date: string; messages: number; tool_calls: number; runs: number }[];
  activity_24h: { hour: string; messages: number; tool_calls: number }[];
  activity_range?: { ts: string; bucket: "hour" | "day"; messages: number; tool_calls: number; runs: number }[];
  heatmap?: { matrix: number[][]; max: number };
  top_chats: {
    id: string;
    title: string;
    messages: number;
    tool_calls: number;
    last_activity: string | null;
  }[];
  connectors_detail: { id: string; name: string; type: string; status: string }[];
  azure_posture: {
    workload_total: number;
    assessed_count: number;
    avg_score: number | null;
    pillar_avgs: Record<string, number>;
    findings_by_severity: { critical: number; error: number; warning: number; info: number };
    open_findings: number;
    new_findings: number;
    top_failing: { title: string; pillar: string; severity: Severity; count: number; resources: number }[];
    workloads: {
      workload_id: string;
      workload_name: string;
      run_id: string;
      overall_score: number | null;
      failed: number;
      severity: Severity;
      pillars: string[];
      at: string | null;
    }[];
    workload_options?: { workload_id: string; workload_name: string }[];
    selected_workload_id?: string;
    last_assessed_at: string | null;
  };
  automations: {
    active: number;
    total: number;
    runs_total: number;
    runs_by_status: Record<string, number>;
    recent_runs: {
      task_id: string | null;
      thread_id: string | null;
      task_name: string | null;
      status: string;
      trigger: string;
      summary: string;
      error: string;
      duration_ms: number | null;
      started_at: string;
    }[];
    upcoming: { id: string; name: string; next_run_at: string | null }[];
  };
  recent_activity: {
    id: string;
    actor_id: string;
    action: string;
    target: string | null;
    provider: string | null;
    model: string | null;
    chat_id: string | null;
    created_at: string;
  }[];
}

/** One tile placement on a Monitor dashboard's 12-column grid. */
export interface MonitorTilePlacement {
  tileId: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A saved, customizable Monitor dashboard (Azure-Dashboard style). */
export interface MonitorDashboard {
  id: string;
  name: string;
  description: string;
  tenant_id?: string;
  is_default: boolean;
  tiles: MonitorTilePlacement[];
  widgets: MonitorWidget[];
  params: MonitorDashboardParam[];
  workload_id?: string;
  version?: number;
  revisions?: MonitorDashboardRevision[];
  created_by?: string;
  updated_by?: string;
  created_at?: string;
  updated_at?: string;
}

// ---- Monitor 2.0: widgets, datasources, results ----

export interface MonitorWidgetLayout {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type MonitorWidgetType =
  | "stat" | "chart" | "table" | "list" | "gauge" | "map"
  | "markdown" | "clock" | "availability" | "builtin";

export interface MonitorDataSource {
  kind: string;
  connection_id?: string;
  query?: string;
  workspace_id?: string;
  timespan?: string;
  resource_ids?: string[];
  metrics?: string[];
  aggregation?: string;
  interval?: string;
  url?: string;
  method?: string;
  host?: string;
  port?: number;
  assert_status?: number;
  assert_body?: string;
  workbook_id?: string;
  telemetry_key?: string;
  rows?: unknown;
  columns?: unknown;
  [key: string]: unknown;
}

export interface MonitorWidget {
  id: string;
  title: string;
  type: MonitorWidgetType;
  tileId?: string;
  layout: MonitorWidgetLayout;
  dataSource: MonitorDataSource;
  transform: Record<string, unknown>;
  viz: Record<string, unknown>;
  refresh: { mode: "live" | "manual"; intervalSec: number };
  links: Record<string, unknown>;
  conditional: unknown[];
}

export interface MonitorDashboardParam {
  key: string;
  label: string;
  type: string;
  default?: unknown;
  options?: unknown[];
}

export interface MonitorDashboardRevision {
  version: number;
  at: string;
  by: string;
  name: string;
  widgets: MonitorWidget[];
  tiles: MonitorTilePlacement[];
  params: MonitorDashboardParam[];
}

export interface WidgetColumn {
  name: string;
  type: string;
}

export interface WidgetTableResult {
  columns: WidgetColumn[];
  rows: unknown[][];
  meta: Record<string, unknown>;
  error: string;
}

// The data behind a chat ```azchart block, fetched by chart_id.
export interface ChartArtifact {
  spec: Record<string, unknown>;
  result: WidgetTableResult;
}

export interface MonitorDatasourceField {
  key: string;
  label: string;
  type: string;
  required?: boolean;
  options?: string[];
  placeholder?: string;
  default?: string;
}

export interface MonitorDatasourceDef {
  kind: string;
  label: string;
  group: string;
  description: string;
  azure: boolean;
  fields: MonitorDatasourceField[];
}

export interface MonitorWidgetTypeDef {
  type: string;
  label: string;
  icon: string;
  desc: string;
  chartTypes?: string[];
}

export interface MonitorDashboardSuggestion {
  widgets: { title: string; type: string; why?: string; dataSource?: MonitorDataSource }[];
  summary?: string;
  workload_name?: string;
  used_memory?: boolean;
  design_brief?: Record<string, unknown>;
  archetype?: string;
  error?: string;
}

export interface MonitorDashboardBuildResult {
  dashboard: Partial<MonitorDashboard>;
  saved_dashboard?: MonitorDashboard;
  used_memory: boolean;
  widget_count: number;
  design_brief?: Record<string, unknown>;
  critic?: Record<string, unknown>;
  dry_runs?: Record<string, unknown>[];
  error?: string;
}

export interface AiPrompt {
  id: string;
  label: string;
  group: string;
  kind: "guidance" | "list";
  description: string;
  current: string;
  default: string;
  contract: string;
  is_overridden: boolean;
}

export type DemoStatus = {
  loaded: boolean;
  present: Record<string, boolean>;
};

export type DemoResult = {
  ok: boolean;
  seeded?: string[];
  removed?: Record<string, number | boolean>;
  errors: Record<string, string>;
  status: DemoStatus;
};

export type AppMeta = {
  name: string;
  version: string;
  environment: string;
};

export type AppStatusCheck = { ok: boolean; label: string; count?: number };
export type AppStatus = {
  name: string;
  version: string;
  environment: string;
  uptime_seconds: number;
  checks: Record<string, AppStatusCheck>;
};

export interface AppSettings {
  custom_instructions: string;
  response_style: string;
  max_tokens: number;
  auto_title: boolean;
  scope_clarification: boolean;
  mgmt_group_clarification: boolean;
  propose_problems: boolean;
  suggestions: boolean;
  deep_parallel_enabled: boolean;
  deep_parallel_count: number;
  progress_detail: "compact" | "normal" | "detailed";
  retention_days: number;
  mcp_read_only: boolean;
  entra_mcp_enabled?: boolean;
  auto_execute_writes: boolean;
  max_tool_iterations: number;
  tool_result_limit: number;
  tool_discovery_limit: number;
  request_timeout_seconds: number;
  command_execution_enabled: boolean;
  command_allowlist: string[];
  command_timeout_seconds: number;
  builtin_tools_enabled: boolean;
  builtin_tools_disabled: string[];
  network_egress_denylist: string[];
  network_egress_allowlist: string[];
  network_tool_timeout_seconds: number;
  assessment_severity_weights: Record<string, number>;
  assessment_score_good: number;
  assessment_score_warn: number;
  workload_health_weights?: Record<string, number>;
  workload_nightly_refresh?: boolean;
  architecture_category_colors: Record<string, string>;
  radar_cache_ttl_s?: number;
  radar_digest_lead_days?: number[];
  radar_azure_updates_feed_enabled?: boolean;
  radar_azure_updates_feed_url?: string;
  // Policy exemption guardrails.
  policy_exemption_require_justification?: boolean;
  policy_exemption_max_expiry_days?: number;
  policy_exemption_block_never_expires?: boolean;
  // Change Explorer.
  changeexplorer_resolve_identities?: boolean;
  changeexplorer_change_limit?: number;
}

export interface ChatgptStatus {
  signed_in: boolean;
  has_token: boolean;
  expired: boolean;
  account_id: string;
}

export interface ClaudeOauthStatus {
  signed_in: boolean;
  has_token: boolean;
  expired: boolean;
  account_id: string;
}

export interface GithubCopilotStatus {
  signed_in: boolean;
  has_token: boolean;
  expired: boolean;
  api_base_url: string;
  expires_at: string;
}

export interface ProviderConfig {
  model: string;
  base_url: string;
  api_version: string;
  free_only?: boolean;
  disabled?: boolean;
  hidden_models?: string[];
  has_key: boolean;
  key_hint: string;
}

export interface LLMConfig {
  active_provider: string;
  providers: Record<string, ProviderConfig>;
}

export interface LLMConfigUpdate {
  active_provider?: string;
  providers?: Record<
    string,
    {
      model?: string;
      api_key?: string;
      base_url?: string;
      api_version?: string;
      free_only?: boolean;
      disabled?: boolean;
      hidden_models?: string[];
    }
  >;
}

export interface SubscriptionOption {
  id: string;
  name: string;
  is_default: boolean;
}

export interface ManagementGroupOption {
  id: string;
  name: string;
}

// --- Azure Workloads ---
export type WorkloadNodeKind = "mg" | "subscription" | "resource_group" | "resource";

export interface WorkloadNode {
  kind: WorkloadNodeKind;
  id: string;
  name: string;
  subscription_id?: string | null;
  resource_group?: string | null;
  resource_type?: string | null;
  location?: string | null;
  excludes?: string[];
}

export interface Workload {
  id: string;
  name: string;
  description: string;
  connection_id: string;
  tenant_id: string;
  nodes: WorkloadNode[];
  tags: string[];
  origin?: { kind?: string; id?: string; name?: string };
  summary?: WorkloadSummary;
  reasoning?: string;
  confidence?: number;
  workload_type?: string;
  environment?: string;
  criticality?: string;
  data_classification?: string;
  evidence?: WorkloadEvidence[];
  last_refreshed?: string;
  created_at?: string;
  updated_at?: string;
  deleted_at?: string;
  // Workload Group ("application" / service family) this workload belongs to, or "" / undefined
  // when ungrouped. See the WorkloadGroup type + /workloads/groups endpoints.
  group_id?: string;
}

export interface WorkloadEvidence {
  kind: string; // provenance | network | scope | rbac
  detail: string;
}

// ---- Workload Groups (applications / service families) ---------------------------
// A non-destructive association over workloads that keep their own identity — e.g. a "CRM"
// group containing the separate "CRM PROD" and "CRM DEV" workloads. Distinct from merge
// (which fuses workloads into one) and overlaps (shared resources).
export interface WorkloadGroupBase {
  id: string;
  name: string;
  description: string;
  color?: string;
  owner?: string;
  tags: string[];
  tenant_id?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface WorkloadGroupRollup {
  member_count: number;
  analyzed_count: number;
  total_resources: number;
  health: { avg_score: number | null; band: "good" | "warn" | "poor" | "unknown"; distribution: Record<string, number> };
  criticality: string;
  risk: { retirements_90d: number; criticals: number };
  by_category: { category: string; count: number }[];
  by_environment: { environment: string; count: number }[];
}

export interface WorkloadGroupMemberRef {
  id: string;
  name: string;
  environment?: string;
  criticality?: string;
  connection_id?: string;
}

export interface WorkloadGroup extends WorkloadGroupBase {
  member_ids: string[];
  members: WorkloadGroupMemberRef[];
  member_count: number;
  rollup: WorkloadGroupRollup;
}

export interface WorkloadGroupSuggestion {
  name: string;
  stem: string;
  workload_ids: string[];
  members: { id: string; name: string; environment?: string }[];
}

export interface WorkloadGroupDetail {
  group: WorkloadGroupBase;
  members: Workload[];
  profiles: WorkloadProfile[];
  rollup: WorkloadGroupRollup;
}

// ---- Group compare (PROD-vs-DEV drift) -------------------------------------------
export interface WorkloadGroupCompareMember {
  id: string;
  name: string;
  environment: string;
  criticality: string;
  data_classification: string;
  workload_type: string;
  total_resources: number;
  health_score: number | null;
  health_band: "good" | "warn" | "poor" | "unknown";
  retirements_90d: number;
  criticals: number;
  analyzed: boolean;
}
export interface WorkloadGroupCompareSignal {
  key: string;
  label: string;
  values: Record<string, number | null>; // member_id -> signal score
  drift: boolean;
}
export interface WorkloadGroupCompareCategory {
  category: string;
  counts: Record<string, number>; // member_id -> count (absent = 0)
  present_in: number;
  total: number;
  drift: boolean;
}
export interface WorkloadGroupCompareType {
  type: string;
  friendly: string;
  counts: Record<string, number>; // member_id -> count (absent = 0)
  present_in: number;
  total: number;
  drift: boolean;
}
export interface WorkloadGroupCompare {
  members: WorkloadGroupCompareMember[];
  signals: WorkloadGroupCompareSignal[];
  categories: WorkloadGroupCompareCategory[];
  types: WorkloadGroupCompareType[];
  highlights: string[];
  summary: {
    member_count: number;
    drift_types: number;
    drift_categories: number;
    drift_signals: number;
    health_spread: number;
  };
}
export interface WorkloadGroupCompareResult {
  group: WorkloadGroupBase;
  compare: WorkloadGroupCompare;
}

// ---- Workload overlaps (resources shared across multiple workloads) --------------
export interface OverlapWorkloadRef {
  id: string;
  name: string;
  via: "explicit" | "resource_group" | "subscription" | "mg";
}
export interface WorkloadOverlapRow {
  id: string;
  name: string;
  resource_type: string;
  friendly_type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  count: number;
  all_explicit: boolean;
  workloads: OverlapWorkloadRef[];
}
export interface WorkloadOverlapPair {
  a: { id: string; name: string };
  b: { id: string; name: string };
  shared_count: number;
}
export interface WorkloadOverlaps {
  overlaps: WorkloadOverlapRow[];
  summary: {
    duplicated_resources: number;
    workloads_involved: number;
    total_extra_memberships: number;
    by_type: { friendly_type: string; count: number }[];
  };
  by_pair: WorkloadOverlapPair[];
  generated_at: string;
  deep: boolean;
  truncated: boolean;
}

// ---- Workload command-center profile (cache-only rollup) ------------------------
export interface WorkloadProfileComposition {
  total: number;
  scope_counts: { mg?: number; subscription?: number; resource_group?: number; resource?: number };
  by_category: { category: string; count: number }[];
  by_type: { type: string; friendly: string; count: number }[];
  by_location: { location: string; count: number }[];
  by_subscription: { subscription_id: string; count: number }[];
}

export interface WorkloadProfileHealth {
  monitoring: number | null;
  telemetry: number | null;
  backupdr: number | null;
  performance: number | null;
  ownership: number | null;
  policy: number | null;
  tags: number | null;
  score: number | null;          // composite 0-100, null if nothing analyzed
  band: "good" | "warn" | "poor" | "unknown";
  contributing: string[];
  missing: string[];
  weights: Record<string, number>;
  extras: Record<string, Record<string, number | null>>;
}

export interface WorkloadProfile {
  id: string;
  name: string;
  connection_id: string;
  classification: {
    workload_type: string;
    environment: string;
    criticality: string;
    data_classification: string;
  };
  composition: WorkloadProfileComposition;
  health: WorkloadProfileHealth;
  risk: {
    retirements_90d: number | null;
    retirements_total: number | null;
    criticals: number | null;
  };
  activity: { last_refreshed: string; last_refreshed_age_s: number | null; updated_at: string };
  freshness: Record<string, number | null>;
  score_trend: { points: number[]; current: number | null; previous: number | null; delta: number | null; count: number };
  analyzed: boolean;
}

export interface WorkloadHealthWeights {
  signals: string[];
  weights: Record<string, number>;
  bands: { good: number; warn: number };
  nightly_refresh: boolean;
}

export interface TypeCount {
  label: string;
  count: number;
}

export interface WorkloadSummary {
  types: TypeCount[];
  total_resources: number;
  scope_counts: { mg: number; subscription: number; resource_group: number; resource: number };
}

/** A workload proposed by Autopilot, pending the user's confirmation to save. */
export interface WorkloadCandidate {
  name: string;
  description: string;
  reasoning: string;
  confidence: number;
  resource_count: number;
  types: TypeCount[];
  resource_groups: string[];
  nodes: WorkloadNode[];
  workload_type?: string;
  environment?: string;
  criticality?: string;
  data_classification?: string;
  evidence?: WorkloadEvidence[];
}

export interface EstateCoverage {
  connection_id: string;
  total: number;
  organized: number;
  orphaned: number;
  organized_pct: number;
  truncated: boolean;
  orphan_resource_groups: { resource_group: string; count: number }[];
  orphans: {
    id: string;
    name: string;
    resource_type: string;
    resource_group: string;
    subscription_id: string;
    location: string;
  }[];
}

export interface WorkloadRefreshDiff {
  added: WorkloadNode[];
  removed: WorkloadNode[];
  added_count: number;
  removed_count: number;
  scanned_resource_groups: number;
}

export interface AutopilotHandlers {
  onStatus?: (data: { phase: string; message: string; [k: string]: unknown }) => void;
  onCandidate?: (data: { candidate: WorkloadCandidate; message: string }) => void;
  onDone?: (data: { candidates: WorkloadCandidate[]; meta: Record<string, unknown> }) => void;
  onError?: (msg: string) => void;
}

// === Scope Sculptor (Autopilot pre-flight) =================================
export interface FacetCount {
  label: string;
  count: number;
}

export interface NamingConvention {
  delimiter: string;
  segments: number;
  confidence: number;
  pattern: string;
  examples: string[];
}

export interface EstateFacets {
  total: number;
  types: FacetCount[];
  resource_groups: FacetCount[];
  regions: FacetCount[];
  subscriptions: FacetCount[];
  tag_keys: FacetCount[];
  environments: FacetCount[];
  distinct_resource_groups: number;
  distinct_regions: number;
  distinct_subscriptions: number;
  noise_count: number;
  system_rg_count: number;
  naming: NamingConvention;
}

export interface CostEstimate {
  ai_calls: number;
  unit: string;
  effective_resources: number;
  tag_seeded: number;
  est_seconds: number;
  est_tokens: number;
  capped: boolean;
}

export interface FilterPreview {
  kept: number;
  removed: number;
  reasons: Record<string, number>;
  tag_seeded?: number;
}

export interface SurveyResult {
  facets: EstateFacets;
  filter_preview: FilterPreview;
  estimate: CostEstimate;
  meta: { resource_count: number; truncated: boolean; subscriptions: number; scope_name?: string };
}

export interface AutopilotEstimateResult {
  estimate: CostEstimate;
  filter_preview: FilterPreview;
  truncated: boolean;
}

/** The full set of sculpt controls sent to discover / estimate / saved in a profile. */
export interface SculptConfig {
  strategy?: string;
  mode?: string;
  tag_key?: string;
  preset?: string;
  granularity?: string;
  exclude_noise?: boolean;
  exclude_system_rgs?: boolean;
  rg_globs?: string[];
  tag_seed_keys?: string[];
  include_types?: string[];
  exclude_types?: string[];
  environments?: string[];
  regions?: string[];
  subscriptions?: string[];
  name_contains?: string;
  confidence_floor?: number;
  max_ai_calls?: number;
  naming_hint?: string;
}

export interface DiscoveryProfile {
  id: string;
  name: string;
  config: SculptConfig;
  scope_kind: string;
  scope_id: string;
  scope_name: string;
  created_at: string;
  updated_at: string;
  created_by?: string;
  updated_by?: string;
}

export interface SurveyHandlers {
  onStatus?: (data: { phase: string; message: string; [k: string]: unknown }) => void;
  onSurvey?: (data: SurveyResult) => void;
  onError?: (msg: string) => void;
}

/** Stream the Autopilot pre-flight SURVEY (estate facets + default estimate, no AI). */
export async function streamSurvey(
  body: { connection_id: string; scope_kind: string; scope_id: string; scope_name: string },
  handlers: SurveyHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/workloads/autopilot/survey`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "survey") handlers.onSurvey?.(parsed as unknown as SurveyResult);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Survey failed.");
    }
  }
}

export interface PrefetchProgress {
  phase: string;
  message: string;
  subscriptions?: number;
  resource_groups?: number;
  resources?: number;
}

export interface PrefetchHandlers {
  onStatus?: (data: PrefetchProgress) => void;
  onDone?: (data: { subscriptions: number; resource_groups: number; resources: number }) => void;
  onError?: (msg: string) => void;
}

/** Stream cache-prefetch progress (warms the discovery tree) over SSE. */
export async function streamPrefetch(
  body: { connection_id: string; group_by?: string; refresh?: boolean },
  handlers: PrefetchHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/workloads/cache/prefetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as unknown as PrefetchProgress);
      else if (event === "done")
        handlers.onDone?.(parsed as unknown as { subscriptions: number; resource_groups: number; resources: number });
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Prefetch failed.");
    }
  }
}

/** Stream AI Workload Autopilot discovery progress + candidates over SSE. */
export async function streamAutopilot(
  body: {
    connection_id: string; scope_kind: string; scope_id: string; scope_name: string;
    strategy?: string; mode?: string; tag_key?: string;
  } & SculptConfig,
  handlers: AutopilotHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/workloads/autopilot/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      switch (event) {
        case "status":
          handlers.onStatus?.(parsed as { phase: string; message: string });
          break;
        case "candidate":
          handlers.onCandidate?.(parsed as unknown as { candidate: WorkloadCandidate; message: string });
          break;
        case "done":
          handlers.onDone?.(parsed as unknown as { candidates: WorkloadCandidate[]; meta: Record<string, unknown> });
          break;
        case "error":
          handlers.onError?.((parsed.message as string) ?? "Autopilot failed.");
          break;
      }
    }
  }
}

/** A node in the resource-picker tree (or a search result row). */
export interface TreeNode {
  kind: WorkloadNodeKind;
  id: string;
  name: string;
  subscription_id?: string;
  resource_group?: string;
  resource_type?: string;
  location?: string;
  has_children?: boolean;
  // Hierarchy depth for the flat MG picker (group_by='mg_flat'), used to indent nested groups.
  depth?: number;
}

// === Assessments ===========================================================
export interface AssessmentPillarMeta {
  id: string;
  label: string;
  icon: string;
  check_count: number;
}

export interface AssessmentCheckMeta {
  id: string;
  pillar: string;
  title: string;
  description: string;
  severity: Severity;
  weight: number;
  resource_types: string[];
  frameworks: { cis?: string[]; nist?: string[]; iso?: string[]; mcsb?: string[]; pci?: string[] };
  remediation: string;
  remediation_command: string;
  metric_backed?: boolean;
  custom?: boolean;
  kql?: string;
  enabled?: boolean;
  kind?: string; // graph | metric | manual | signal
  impact?: string; // high | medium | low
  effort?: string; // low | medium | high
  sub_category?: string; // WAF sub-pillar (reliability)
  source?: string; // built-in | aprl | advisor | custom | cis-v5
  learn_more?: string[];
  profile?: string; // CIS profile level: L1 | L2
}

export interface AssessmentPack {
  id: string;
  label: string;
  short: string;
  icon: string;
  pillars: string[];
  description: string;
}

export interface AssessmentCatalog {
  pillars: AssessmentPillarMeta[];
  packs?: AssessmentPack[];
  sub_categories?: string[];
  checks: Record<string, AssessmentCheckMeta[]>;
  frameworks?: Record<string, { label: string; icon: string }>;
  score_bands?: { good: number; warn: number };
}

// --- Architectures ---
export interface ArchNode {
  id: string;
  arm_id: string;
  name: string;
  type: string;
  category: string;
  layer: string;
  resource_group: string;
  subscription_id: string;
  location: string;
  sku: string;
  meta: Record<string, string>;
  group_id: string;
  x: number;
  y: number;
}

export type ArchEdgeKind = "depends_on" | "connects_to" | "data_flow" | "network" | "identity" | "monitors";

export interface ArchEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: ArchEdgeKind;
  dashed: boolean;
}

export interface ArchGroup {
  id: string;
  name: string;
  kind: "subscription" | "resource_group" | "vnet" | "tier" | "custom";
  color: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Architecture {
  id: string;
  name: string;
  description: string;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  tenant_id: string;
  source: "manual" | "ai";
  state: ArchitectureState;
  category_id: string;
  nodes: ArchNode[];
  edges: ArchEdge[];
  groups: ArchGroup[];
  ai?: { rationale?: string; confidence?: number | null; resource_count?: number };
  state_changed_by?: string;
  state_changed_at?: string;
  deleted_at?: string;
  created_by?: string;
  updated_by?: string;
  created_at?: string;
  updated_at?: string;
}

export type ArchitectureState = "draft" | "in_review" | "ready" | "archived";

/** Drift report: diagram resources vs. live Azure Resource Graph inventory. */
export interface ArchitectureDrift {
  checked_at: string;
  live_count: number;
  diagram_count: number;
  matched: number;
  in_sync: boolean;
  removed: { id: string; name: string; type: string; arm_id: string }[];
  added: { name: string; type: string; arm_id: string; resource_group: string }[];
}/** Lightweight metadata for one auto-saved revision of an architecture. */
export interface ArchitectureRevision {
  id: string;
  created_at: string;
  by: string;
  reason: string;
  state: ArchitectureState;
  category_id: string;
  node_count: number;
  edge_count: number;
}

/** Full stored content of a single revision (for read-only preview). */
export interface ArchitectureRevisionContent {
  id: string;
  created_at: string;
  by: string;
  reason: string;
  name: string;
  description?: string;
  source: "manual" | "ai";
  state: ArchitectureState;
  category_id: string;
  nodes: ArchNode[];
  edges: ArchEdge[];
  groups: ArchGroup[];
  ai?: Architecture["ai"];
}

/** One entry in an architecture's management activity log (audit trail). */
export interface ArchitectureActivity {
  id: string;
  at: string;
  by: string;
  event: string;
  detail: string;
  meta: Record<string, unknown>;
}

/** A user-managed grouping of whole architectures (a.k.a. Category / Solution). */
export interface ArchitectureCollection {
  id: string;
  name: string;
  description: string;
  color: string;
  icon: string;
  order: number;
  tenant_id?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ArchitectureCategory {
  id: string;
  label: string;
  color: string;
}
export interface ArchitecturePaletteItem {
  type: string;
  label: string;
  category: string;
}
export interface ArchitectureCatalog {
  categories: ArchitectureCategory[];
  layers: string[];
  palette: ArchitecturePaletteItem[];
}

/** A background AI architecture-generation job (one per workload). */
export interface ArchitectureJob {
  id: string;
  workload_id: string;
  workload_name: string;
  status: "queued" | "running" | "done" | "error" | "canceled";
  phase: "queued" | "scope" | "query" | "ai" | "save" | "done";
  progress: number;
  message: string;
  architecture_id: string;
  architecture_name: string;
  resource_count: number;
  // Set when this job is a "rebuild" targeting an existing architecture in place.
  target_architecture_id?: string;
  error: string;
  created_at: string;
  started_at: string;
  ended_at: string;
}

/** Architecture Memory — a Memory.md-style knowledge base owned by an architecture. */
export interface MemorySectionMeta {
  group: string;
  key: string;
  label: string;
  hint: string;
}

export interface MemorySection {
  key: string;
  label: string;
  content: string;
  needs_review?: boolean;
}

export interface ArchitectureMemory {
  id: string;
  architecture_id: string;
  workload_id: string;
  title: string;
  sections: MemorySection[];
  enabled_for_investigations: boolean;
  source: "manual" | "ai" | "hybrid";
  ai?: { confidence?: number | null; generated_at?: string; generated_by?: string; resource_count?: number };
  created_by?: string;
  updated_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ArchitectureMemoryResponse {
  memory: ArchitectureMemory | null;
  markdown: string;
  architecture: {
    id: string;
    name: string;
    workload_id: string;
    workload_name: string;
    updated_at?: string;
    ai?: { rationale?: string; confidence?: number | null; resource_count?: number } | null;
  };
}

export interface MemoryIndexEntry {
  id: string;
  architecture_id: string;
  architecture_name: string;
  architecture_exists: boolean;
  workload_id: string;
  workload_name: string;
  title: string;
  section_count: number;
  filled_count: number;
  enabled_for_investigations: boolean;
  source: "manual" | "ai" | "hybrid";
  updated_at: string;
  updated_by: string;
}

export interface MemoryRevision {
  id: string;
  created_at: string;
  by: string;
  reason: string;
  title: string;
  source: "manual" | "ai" | "hybrid";
  section_count: number;
  filled_count: number;
}

export interface MemoryRevisionContent extends MemoryRevision {
  sections: MemorySection[];
  enabled_for_investigations: boolean;
  ai?: ArchitectureMemory["ai"];
}

// ----- Workload Know-Me -----
export interface KnowMeSection {
  key: string;
  label: string;
  content: string;
}
export interface KnowMeTodo {
  id: string;
  field_key: string;
  label: string;
  section_key: string;
  status: "open" | "done";
  value: string;
  type: "email" | "person" | "group" | "duration" | "datetime" | "number" | "url" | "text";
  required: boolean;
  group: string;
  suggestions: string[];
  source: "human" | "auto" | "suggested";
  confidence?: number | null;
  assignee?: string;
  note?: string;
  // Choice set: candidate values offered as a dropdown / segmented control. ``allow_custom``
  // decides picker (free text allowed) vs strict select. ``choice_source`` is provenance.
  choices?: string[];
  allow_custom?: boolean;
  choice_source?: "" | "platform" | "rule" | "ai";
  multi?: boolean;
}
export interface KnowMeAsset {
  id: string;
  filename: string;
  content_type: string;
  size: number;
}
export interface KnowMe {
  id: string;
  architecture_id: string;
  workload_id: string;
  workload_name: string;
  title: string;
  description?: string;
  sections: KnowMeSection[];
  todos: KnowMeTodo[];
  assets?: KnowMeAsset[];
  status: "draft" | "in_review" | "published" | "archived";
  is_reference?: boolean;
  source: "ai" | "edited" | "hybrid";
  confidence?: number | null;
  ai?: {
    confidence?: number | null;
    passes?: number;
    autofilled?: number;
    evidence_used?: {
      assessment?: boolean;
      assessment_findings?: number;
      coverage?: string[];
      performance?: boolean;
      idle_resources?: number;
    };
    generated_at?: string;
    generated_by?: string;
  };
  updated_at?: string;
  updated_by?: string;
}
export interface KnowMeResponse {
  id?: string;
  know_me: KnowMe | null;
  markdown: string;
  has_memory?: boolean;
  memory_updated_at?: string;
  architecture: ArchitectureMemoryResponse["architecture"];
}
export interface KnowMeRevision {
  id: string;
  created_at: string;
  by: string;
  reason: string;
  title: string;
  status: string;
  source: string;
  section_count: number;
  filled_count: number;
  open_todos: number;
}
export interface KnowMeRevisionContent extends KnowMeRevision {
  sections: KnowMeSection[];
  todos: KnowMeTodo[];
}
/** One Know-Me document in the standalone index (a workload may have many). */
export interface KnowMeDocument {
  id: string;
  architecture_id: string;
  architecture_name: string;
  architecture_exists: boolean;
  workload_id: string;
  workload_exists?: boolean;
  workload_name: string;
  title: string;
  description?: string;
  status: "draft" | "in_review" | "published" | "archived";
  is_reference?: boolean;
  source: "" | "ai" | "edited" | "hybrid";
  section_count: number;
  filled_count: number;
  open_todos: number;
  updated_at: string;
  updated_by: string;
  deleted_at?: string;
}
/** An architecture (with a Memory) the user can build a new Know-Me from. */
export interface KnowMeBuildable {
  architecture_id: string;
  architecture_name: string;
  architecture_exists: boolean;
  workload_id: string;
  workload_exists?: boolean;
  workload_name: string;
  know_me_count: number;
}
export interface KnowMeIndex {
  documents: KnowMeDocument[];
  buildable: KnowMeBuildable[];
  trash_count: number;
}
/** A detached "Build from workload" job (Architecture → Memory → Know-Me). Survives navigation;
 *  the index polls these to show background progress and offer an Open link on completion. */
export interface KnowMeBuildJob {
  id: string;
  key: string;
  status: "running" | "done" | "error";
  started_at: string;
  finished_at: string | null;
  progress_count: number;
  last_message: string;
  error: string;
  workload_id: string;
  workload_name: string;
  result: { id: string } | null;
}
/** A soft-deleted Know-Me in the Trash. */
export interface KnowMeTrashEntry {
  id: string;
  architecture_id: string;
  workload_name: string;
  title: string;
  status: string;
  deleted_at: string;
  deleted_by: string;
  updated_at: string;
}

/** Stream a Quota scan (SSE: status… → done). Mirrors the FMEA generation stream so the UI can
 *  show a live activity-log popup. The final `done` event carries the decorated QuotaSnapshot. */
export async function streamQuotaScan(
  p: QuotaScanParams,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (snap: QuotaSnapshot) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const params = new URLSearchParams();
  params.set("subscription_id", p.subscription_id);
  params.set("demo", p.demo ? "true" : "false");
  if (p.connection_id) params.set("connection_id", p.connection_id);
  if (p.regions && p.regions.length) params.set("regions", p.regions.join(","));
  if (p.categories && p.categories.length) params.set("categories", p.categories.join(","));
  if (p.include_unused) params.set("include_unused", "true");

  const res = await fetch(`${API_BASE}/quota/scan/stream?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: "{}",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as QuotaSnapshot);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Scan failed.");
    }
  }
}

/** Stream the AI Know-Me generation for an architecture (SSE: status… → done). */
export async function streamGenerateKnowMe(
  architectureId: string,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: KnowMeResponse) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/architectures/${architectureId}/know-me/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ extra_context: extraContext }),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as KnowMeResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

/** Regenerate an EXISTING Know-Me document from its architecture's Memory (SSE → done).
 *  Keyed by km_id. */
export async function streamRegenerateKnowMe(
  kmId: string,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: KnowMeResponse) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/architectures/know-me/${kmId}/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ extra_context: extraContext }),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as KnowMeResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

/** Regenerate ONE Know-Me section with AI, streaming detailed status. SSE: status…
 *  (phase=scope|evidence|ai|save) → done{KnowMeResponse}. Keyed by km_id. */
export async function streamRegenerateKnowMeSection(
  kmId: string,
  sectionKey: string,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: KnowMeResponse) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/architectures/know-me/${kmId}/sections/${encodeURIComponent(sectionKey)}/generate/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ extra_context: extraContext }),
      signal,
    },
  );
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as KnowMeResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

/** One-click pipeline: from an Azure workload, ensure an architecture + its Memory exist
 *  (building them with AI if missing), then transform the Memory into a Know-Me.
 *  SSE: status… (phase=architecture|memory|knowme|save) → done{KnowMeResponse}. */
export async function streamBuildKnowMeFromWorkload(
  body: { workload_id: string; connection_id?: string | null; architecture_id?: string | null; extra_context?: string },
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: KnowMeResponse) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/architectures/know-me/from-workload/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as KnowMeResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

/** Stream the AI memory generation for an architecture (SSE: status… → done). */
export async function streamGenerateMemory(
  architectureId: string,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: ArchitectureMemoryResponse) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/architectures/${architectureId}/memory/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ extra_context: extraContext }),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as ArchitectureMemoryResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

/** Stream the AI reverse-engineering of an architecture from a workload (SSE). */
export async function streamArchitectureFromWorkload(
  body: { workload_id: string; connection_id?: string | null; save?: boolean },
  handlers: {
    onStatus?: (d: { phase: string; message: string }) => void;
    onDone?: (d: { architecture: Architecture }) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/architectures/from-workload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as { architecture: Architecture });
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}


export interface AssessmentPillarScore {
  score: number | null;
  worst_case_score?: number | null;
  passed: number;
  failed: number;
  na: number;
  waived?: number;
  errored: number;
  manual?: number;
  total: number;
}

export interface AssessmentTotals {
  passed: number;
  failed: number;
  na: number;
  waived?: number;
  errored?: number;
  manual?: number;
  evaluated?: number;
  evaluatable?: number;
  completeness_pct?: number;
  confidence?: string;
  by_severity: Record<string, number>;
}

export interface AssessmentDiff {
  baseline_run_id: string;
  baseline_is_pinned?: boolean;
  new_failures: ({ title: string; severity: Severity } | string)[];
  new_criticals?: number;
  resolved: string[];
  score_delta: Record<string, { before: number | null; after: number | null }>;
}

export interface AssessmentFlaggedResource {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  subscription_id: string;
  remediation_command?: string;
}

export interface AssessmentFinding {
  check_id: string;
  pillar: string;
  title: string;
  description: string;
  severity: Severity;
  weight: number;
  frameworks: { cis?: string[]; nist?: string[]; iso?: string[]; mcsb?: string[]; pci?: string[] };
  remediation: string;
  remediation_command: string;
  resource_types: string[];
  status: "pass" | "fail" | "not_applicable" | "error" | "waived" | "manual";
  flagged_count: number;
  flagged_resources: AssessmentFlaggedResource[];
  ai_rationale?: string;
  error?: string;
  partial?: boolean;
  kind?: string;
  impact?: string;
  effort?: string;
  sub_category?: string;
  source?: string;
  learn_more?: string[];
  profile?: string;
  attestation?: { status: string; note?: string; by?: string; at?: string };
  waiver?: { justification: string; approver: string };
}

export interface AssessmentComplianceControl {
  control: string;
  status: string;
  checks: { check_id: string; title: string; status: string }[];
}
export interface AssessmentComplianceFramework {
  label: string;
  icon: string;
  controls: AssessmentComplianceControl[];
  total: number;
  passed: number;
  failed: number;
  coverage: number | null;
}

export interface AssessmentTrendPoint {
  run_id: string;
  at: string | null;
  overall: number | null;
  scores: Record<string, number | null>;
}

export interface AssessmentPortfolioRow {
  workload_id: string;
  workload_name: string;
  run_id: string;
  overall_score: number | null;
  scores: Record<string, number | null>;
  failed: number;
  severity: Severity;
  at: string | null;
  sparkline: number[];
}

export interface AssessmentWaiver {
  id: string;
  workload_id: string;
  check_id: string;
  resource_id: string | null;
  justification: string;
  approver: string;
  status: string;
  expires_at: string | null;
  created_by: string;
  created_at: string | null;
}

export interface AssessmentFindingStateT {
  workload_id: string;
  check_id: string;
  status: string;
  assignee: string | null;
  due_date: string | null;
  notes: string | null;
  ticket_url: string | null;
  ticket_id: string | null;
  ticket_connector: string | null;
  updated_by: string;
  updated_at: string | null;
}

export interface AssessmentSchedule {
  id: string;
  name: string;
  workload_id: string;
  workload_name: string;
  connection_id: string;
  pillars: string[];
  use_ai: boolean;
  enabled: boolean;
  schedule_kind: string;
  time_of_day: string;
  weekday: number;
  cron_expr: string;
  timezone: string;
  alert_on_new_findings: boolean;
  alert_min_severity: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_score: number | null;
}

export interface AssessmentRunSummary {
  id: string;
  workload_id: string;
  workload_name: string;
  connection_id?: string | null;
  pillars: string[];
  status: string;
  overall_score: number | null;
  worst_case_score?: number | null;
  completeness_pct?: number | null;
  confidence?: string | null;
  catalog_version?: string | null;
  schema_version?: number | null;
  scores: Record<string, AssessmentPillarScore>;
  totals: AssessmentTotals;
  severity: Severity;
  summary: string | null;
  used_ai: boolean;
  baseline_run_id: string | null;
  is_baseline?: boolean;
  diff: AssessmentDiff | null;
  trigger: string;
  triggered_by: string;
  duration_ms: number | null;
  started_at: string | null;
  ended_at: string | null;
  deleted_at?: string | null;
  resource_count?: number | null;
}

export interface AssessmentScannedResource {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  subscription_id: string;
  location: string;
}

export interface AssessmentRunDetail extends AssessmentRunSummary {
  findings: AssessmentFinding[];
  resources?: AssessmentScannedResource[];
  compliance?: Record<string, AssessmentComplianceFramework>;
}

export interface AssessmentActionPlanItem {
  rank: number;
  check_id: string;
  title: string;
  pillar: string;
  sub_category?: string;
  severity: Severity;
  impact?: string;
  effort?: string;
  flagged_count: number;
  partial?: boolean;
  priority: number;
  remediation?: string;
  remediation_command?: string;
  ai_rationale?: string;
  source?: string;
}

export interface AssessmentActionPlan {
  run_id: string;
  workload_name: string | null;
  overall_score: number | null;
  completeness_pct?: number | null;
  confidence?: string | null;
  plan: AssessmentActionPlanItem[];
  pending_manual: { check_id: string; title: string; pillar: string; sub_category?: string; severity: Severity; remediation?: string }[];
}

export interface AssessmentResourceRollup {
  id: string;
  name: string;
  type: string;
  resource_group: string;
  subscription_id: string;
  worst_severity: Severity;
  findings: { check_id: string; title: string; pillar: string; severity: Severity; remediation_command?: string }[];
}

export interface AssessmentHandlers {
  onStatus?: (data: { phase: string; message: string; [k: string]: unknown }) => void;
  onCheckStart?: (data: { index: number; total: number; check_id: string; title: string; pillar: string }) => void;
  onCheckResult?: (data: {
    check_id: string;
    title: string;
    pillar: string;
    severity: Severity;
    status: string;
    flagged_count?: number;
  }) => void;
  onDone?: (data: {
    run_id: string;
    overall_score: number | null;
    worst_case_score?: number | null;
    completeness_pct?: number | null;
    confidence?: string | null;
    scores: Record<string, AssessmentPillarScore>;
    totals: AssessmentTotals;
    severity: Severity;
    used_ai: boolean;
    summary: string;
    diff: AssessmentDiff | null;
  }) => void;
  onError?: (msg: string) => void;
}

/** Stream an assessment run over SSE. */
export async function streamAssessment(
  body: { workload_id: string; pillars: string[]; pack?: string | null; connection_id?: string; use_ai?: boolean },
  handlers: AssessmentHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/assessments/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      switch (event) {
        case "status":
          handlers.onStatus?.(parsed as { phase: string; message: string });
          break;
        case "check_start":
          handlers.onCheckStart?.(parsed as never);
          break;
        case "check_result":
          handlers.onCheckResult?.(parsed as never);
          break;
        case "done":
          handlers.onDone?.(parsed as never);
          break;
        case "error":
          handlers.onError?.((parsed.message as string) ?? "Assessment failed.");
          break;
      }
    }
  }
}


/** Cache freshness metadata returned by cached discovery endpoints. */
export interface CacheMeta {
  cached_at: string;
  age_seconds: number;
  from_cache: boolean;
}

// --- Workbooks ---
export type Severity = "info" | "warning" | "error" | "critical";

export interface WorkbookParam {
  key: string;
  label: string;
  type: string; // text | number | select
  default?: unknown;
  required?: boolean;
  help?: string;
}

/** A portable export bundle (workbook or playbook) downloaded as JSON. */
export interface BundleEnvelope {
  format: string;
  version: number;
  kind: "workbook" | "playbook";
  exported_at: string;
  workbook?: Partial<Workbook>;
  playbook?: Partial<Playbook>;
  workbooks?: Partial<Workbook>[];
}
export interface Workbook {
  id: string;
  name: string;
  description: string;
  runtime: "az" | "kql" | "powershell";
  body: string;
  params: WorkbookParam[];
  kind: "read" | "write";
  tags: string[];
  connection_id: string;
  aify: { enabled: boolean; modes: string[]; schema: string };
  alert: { enabled: boolean; min_severity: Severity };
  tile: { enabled: boolean; label: string; format: "severity" | "number" | "text"; metric_key: string };
  enabled: boolean;
  starter?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface WorkbookRunRequest {
  params: Record<string, unknown>;
  connection_id?: string | null;
  confirm?: boolean;
}

/** An AI-generated workbook draft (a Workbook minus server fields, plus rationale). */
export interface WorkbookDraft {
  name: string;
  description: string;
  runtime: "az" | "kql" | "powershell";
  body: string;
  params: WorkbookParam[];
  kind: "read" | "write";
  tags: string[];
  aify: { enabled: boolean; modes: string[]; schema: string };
  alert: { enabled: boolean; min_severity: Severity };
  tile: { enabled: boolean; label: string; format: "severity" | "number" | "text"; metric_key: string };
  enabled: boolean;
  summary?: string;
  rationale?: string;
  changes?: string[];
}

export interface WorkbookPreviewRequest {
  workbook: Partial<Workbook>;
  params: Record<string, unknown>;
  connection_id?: string | null;
  confirm?: boolean;
}

export interface WorkbookRun {
  id: string;
  workbook_id: string;
  workbook_name?: string;
  runtime: string;
  command?: string;
  status: "running" | "succeeded" | "failed";
  exit_code?: number | null;
  output?: string | null;
  structured?: Record<string, unknown> | null;
  narrative?: string | null;
  severity: Severity;
  diff?: {
    changed: Record<string, { from: unknown; to: unknown }>;
    added: string[];
    removed: string[];
    has_changes: boolean;
  } | null;
  error?: string | null;
  duration_ms?: number | null;
  trigger?: string;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface WorkbookTile {
  workbook_id: string;
  label: string;
  format: "severity" | "number" | "text";
  metric_key: string;
  value: unknown;
  severity: Severity | null;
  narrative: string | null;
  ran_at: string | null;
  status: string;
}

// --- Playbooks ---

// ============================================================================
// FMEA — Failure Mode and Effects Analysis (multiple per workload; keyed by
// fmea_id; draft/published + Trash). Each document holds multiple tables of
// scored rows; the Risk Priority Number (RPN) is always derived server-side.
// ============================================================================
export type FmeaRiskBand = "critical" | "high" | "medium" | "low" | "none";

export interface FmeaRow {
  id: string;
  item: string;
  function: string;
  failure_mode: string;
  effects: string;
  causes: string;
  control_prevention: string;
  control_detection: string;
  recommended_actions: string;
  owner: string;
  date_due: string;
  action_results: string;
  date_completed: string;
  severity: number;
  occurrence: number;
  detection: number;
  severity_post: number;
  occurrence_post: number;
  detection_post: number;
  // Derived server-side (read-only).
  rpn: number | null;
  rpn_post: number | null;
  risk_band: FmeaRiskBand;
  risk_band_post: FmeaRiskBand;
}

export interface FmeaTable {
  id: string;
  name: string;
  scope_ref: string;
  rows: FmeaRow[];
}

export interface FmeaDoc {
  id: string;
  architecture_id: string;
  workload_id: string;
  workload_name: string;
  title: string;
  scope_note: string;
  tables: FmeaTable[];
  status: "draft" | "in_review" | "published" | "archived";
  source: "" | "ai" | "edited" | "hybrid";
  ai?: {
    confidence?: number | null;
    passes?: number;
    generated_at?: string;
    generated_by?: string;
  };
  updated_at?: string;
  updated_by?: string;
  deleted_at?: string;
}

export interface FmeaSummary {
  counts: Record<FmeaRiskBand, number>;
  total_rows: number;
  scored_rows: number;
  top_rpn: number;
  mitigated_rows: number;
  open_actions: number;
}

export interface FmeaResponse {
  id?: string;
  fmea: FmeaDoc;
  summary: FmeaSummary;
  has_memory?: boolean;
  memory_updated_at?: string;
  architecture: {
    id: string;
    name: string;
    workload_id: string;
    workload_name: string;
    updated_at: string;
  };
}

export interface FmeaDocumentSummary {
  id: string;
  architecture_id: string;
  architecture_name: string;
  architecture_exists: boolean;
  workload_id: string;
  workload_exists?: boolean;
  workload_name: string;
  title: string;
  status: "draft" | "in_review" | "published" | "archived";
  source: "" | "ai" | "edited" | "hybrid";
  table_count: number;
  row_count: number;
  top_rpn: number;
  counts: Record<FmeaRiskBand, number>;
  updated_at: string;
  updated_by: string;
}

export interface FmeaBuildable {
  architecture_id: string;
  architecture_name: string;
  architecture_exists: boolean;
  workload_id: string;
  workload_exists?: boolean;
  workload_name: string;
  fmea_count: number;
}

export interface FmeaIndex {
  documents: FmeaDocumentSummary[];
  buildable: FmeaBuildable[];
  trash_count: number;
}

export interface FmeaTrashEntry {
  id: string;
  architecture_id: string;
  workload_name: string;
  title: string;
  status: string;
  deleted_at: string;
  deleted_by: string;
  updated_at: string;
}

export interface FmeaRevision {
  id: string;
  created_at: string;
  by: string;
  reason: string;
  title: string;
  status: string;
  source: string;
  table_count: number;
  row_count: number;
}

export type FmeaSavePayload = {
  title?: string;
  scope_note?: string;
  tables?: FmeaTable[];
  status?: string;
};

export const fmea = {
  index: () => http<FmeaIndex>("/fmea"),
  trash: () => http<{ items: FmeaTrashEntry[] }>("/fmea/trash"),
  emptyTrash: () => http<{ purged: number }>("/fmea/trash/empty", { method: "POST", body: "{}" }),
  create: (architectureId: string, title = "") =>
    http<FmeaResponse>("/fmea", { method: "POST", body: JSON.stringify({ architecture_id: architectureId, title }) }),
  get: (fmeaId: string) => http<FmeaResponse>(`/fmea/${fmeaId}`),
  save: (fmeaId: string, body: FmeaSavePayload) =>
    http<FmeaResponse>(`/fmea/${fmeaId}`, { method: "PUT", body: JSON.stringify(body) }),
  remove: (fmeaId: string) => http<{ ok: boolean }>(`/fmea/${fmeaId}`, { method: "DELETE" }),
  restore: (fmeaId: string) => http<{ ok: boolean; fmea: FmeaDoc }>(`/fmea/${fmeaId}/restore`, { method: "POST", body: "{}" }),
  purge: (fmeaId: string) => http<{ ok: boolean }>(`/fmea/${fmeaId}/purge`, { method: "DELETE" }),
  revisions: (fmeaId: string) => http<{ revisions: FmeaRevision[] }>(`/fmea/${fmeaId}/revisions`),
  restoreRevision: (fmeaId: string, revisionId: string) =>
    http<FmeaResponse>(`/fmea/${fmeaId}/revisions/${revisionId}/restore`, { method: "POST", body: "{}" }),
  exportUrl: (fmeaId: string) => `${API_BASE}/fmea/${fmeaId}/export?format=csv`,
  exportXlsxUrl: (fmeaId: string) => `${API_BASE}/fmea/${fmeaId}/export?format=xlsx`,
  // Background generation-job status for an FMEA doc (reconnect on mount).
  generateJob: (fmeaId: string) =>
    http<{ job: { id: string; status: string; last_message: string } | null }>(`/fmea/${fmeaId}/generate/job`),
};

/** Generic SSE reader for the FMEA generation endpoints (status… → done | error). */
async function _consumeFmeaStream(
  res: Response,
  handlers: {
    onStatus?: (s: { phase: string; message: string }) => void;
    onDone?: (r: FmeaResponse) => void;
    onError?: (msg: string) => void;
  },
): Promise<void> {
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b?.detail) detail = b.detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(parsed as { phase: string; message: string });
      else if (event === "done") handlers.onDone?.(parsed as unknown as FmeaResponse);
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Failed.");
    }
  }
}

type FmeaStreamHandlers = {
  onStatus?: (s: { phase: string; message: string }) => void;
  onDone?: (r: FmeaResponse) => void;
  onError?: (msg: string) => void;
};

/** Create a NEW FMEA for an architecture by transforming its Memory (SSE → done). */
export async function streamGenerateFmea(
  architectureId: string,
  handlers: FmeaStreamHandlers,
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/fmea/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ architecture_id: architectureId, extra_context: extraContext }),
    signal,
  });
  await _consumeFmeaStream(res, handlers);
}

/** Regenerate an EXISTING FMEA document from its architecture's Memory (SSE → done). */
export async function streamRegenerateFmea(
  fmeaId: string,
  handlers: FmeaStreamHandlers,
  signal?: AbortSignal,
  extraContext = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/fmea/${fmeaId}/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ extra_context: extraContext }),
    signal,
  });
  await _consumeFmeaStream(res, handlers);
}

/** Regenerate the rows of ONE table from the architecture's Memory (SSE → done). */
export async function streamRegenerateFmeaTable(
  fmeaId: string,
  tableId: string,
  handlers: FmeaStreamHandlers,
  signal?: AbortSignal,
  focus = "",
): Promise<void> {
  const res = await fetch(`${API_BASE}/fmea/${fmeaId}/tables/${tableId}/generate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ focus }),
    signal,
  });
  await _consumeFmeaStream(res, handlers);
}

/** TP4 — live Telemetry-coverage scan over SSE: progress(scanned X of N) → done(snapshot).
 *  Resolves with the final snapshot; the backend scan is shielded so it caches even if aborted. */
export async function streamTelemetryRefresh(
  params: { workload_id?: string; subscription_id?: string; connection_id?: string },
  handlers: {
    onProgress?: (p: { done: number; total: number; resource: string }) => void;
    onDone?: (snap: TelemetryCoverage) => void;
    onError?: (msg: string) => void;
  },
  signal?: AbortSignal,
): Promise<TelemetryCoverage | null> {
  const q = new URLSearchParams();
  if (params.workload_id) q.set("workload_id", params.workload_id);
  if (params.subscription_id) q.set("subscription_id", params.subscription_id);
  if (params.connection_id) q.set("connection_id", params.connection_id);
  const res = await fetch(`${API_BASE}/telemetry/refresh/stream?${q.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: "{}",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b?.detail) detail = b.detail; } catch { /* ignore */ }
    handlers.onError?.(detail);
    return null;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: TelemetryCoverage | null = null;
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try { ({ value, done } = await reader.read()); } catch (err) {
      if ((err as Error)?.name === "AbortError") return final;
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: Record<string, unknown>;
      try { parsed = JSON.parse(data); } catch { continue; }
      if (event === "progress") handlers.onProgress?.(parsed as unknown as { done: number; total: number; resource: string });
      else if (event === "done") { final = parsed as unknown as TelemetryCoverage; handlers.onDone?.(final); }
      else if (event === "error") handlers.onError?.((parsed.message as string) ?? "Scan failed.");
    }
  }
  return final;
}

export interface PlaybookStep {
  id: string;
  name: string;
  workbook_id: string;
  params: Record<string, unknown>;
  param_map: Record<string, string>;
  run_if: "always" | "info" | "warning" | "error" | "critical";
}

export interface Playbook {
  id: string;
  name: string;
  description: string;
  connection_id: string;
  steps: PlaybookStep[];
  alert: { enabled: boolean; min_severity: Severity };
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

/** An AI-generated playbook draft (steps reference existing workbooks). */
export interface PlaybookDraft {
  name: string;
  description: string;
  steps: PlaybookStep[];
  alert: { enabled: boolean; min_severity: Severity };
  enabled: boolean;
  proposed_workbooks?: { title: string; purpose: string }[];
  summary?: string;
  rationale?: string;
}

export interface PlaybookRunResult {
  playbook_id: string;
  name: string;
  status: string;
  severity: Severity;
  steps: PlaybookStepOutcome[];
  run_id?: string;
  duration_ms?: number;
}

export interface PlaybookStepOutcome {
  step_id: string;
  name?: string;
  workbook_id?: string;
  run_id?: string;
  severity?: Severity;
  status?: string;
  narrative?: string;
  skipped?: boolean;
  reason?: string;
  error?: string;
}

/** A persisted playbook run (history). */
export interface PlaybookRun {
  id: string;
  playbook_id: string;
  playbook_name?: string;
  trigger: string;
  status: string;
  severity: Severity;
  steps: PlaybookStepOutcome[];
  step_count: number;
  error?: string | null;
  duration_ms?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
}

// --- Notifications ---
export interface AppNotification {
  id: string;
  type: string;
  source: string;
  severity: Severity;
  title: string;
  body: string;
  facts: Record<string, unknown>;
  links: Record<string, unknown>;
  read: boolean;
  created_at: string | null;
}

export interface NotificationRule {
  id: string;
  name: string;
  enabled: boolean;
  event_types: string[];
  sources: string[];
  min_severity: Severity;
  in_app: boolean;
  connector_ids: string[];
  created_at?: string | null;
  updated_at?: string | null;
}


export interface MessageScope {
  subscription_id?: string;
  subscription_name?: string;
  management_group_id?: string;
  management_group_name?: string;
  connection_id?: string;
  tenant_id?: string;
  tenant_name?: string;
  scope_all?: boolean;
  images?: string[];
  regenerate?: boolean;
  thinking_level?: "normal" | "deep";
  // Specialist investigation agents (ids) dispatched for a deep-investigation war room.
  deep_agents?: string[];
  // Architecture id whose Memory should inform a deep investigation. "" = none/auto.
  architecture_memory_id?: string;
  // Optional custom agent to run this turn as. "" clears any saved agent.
  agent_id?: string;
  // Optional Azure Workload (hand-picked resource scope). "" clears any saved workload.
  workload_id?: string;
}

// One node in the deep-investigation hypothesis tree.
export interface HypothesisNode {
  id: string;
  parent_id: string | null;
  title: string;
  description: string;
  depth: number;
  status: "validating" | "validated" | "invalidated" | "inconclusive";
  evidence: string;
  // Specialist agent (war-room) this hypothesis is attributed to.
  agent?: string;
}

// A specialist investigation agent dispatched in a deep-investigation "war room".
export interface DeepAgent {
  id: string;
  name: string;
  icon: string;
  domain: string;
  // Only present from the suggest endpoint (pre-launch picker).
  recommended?: boolean;
  reason?: string;
}

export interface InvestigationConclusion {
  root_cause: string;
  summary: string;
  evidence: string[];
  actions: string[];
}

export interface Investigation {
  phases?: { phase: string; label: string; summary: string | null }[];
  hypotheses?: HypothesisNode[];
  conclusion?: InvestigationConclusion | null;
  research?: string | null;
  // Specialist agent roster for the war room (when the user dispatched agents).
  agents?: DeepAgent[];
  // Transient live per-agent activity (current tool, counts) shown in the war room
  // while the investigation runs. Not meaningful after completion.
  agentActivity?: Record<string, { tool?: string; busy?: boolean; tools?: number; startedAt?: number }>;
}

export interface DeepInvestigationSummary {
  chat_id: string;
  message_id: string;
  title: string;
  created_at: string;
  provider: string | null;
  model: string | null;
  duration_ms: number | null;
  root_cause: string;
  summary: string;
  hypothesis_counts: { validated: number; invalidated: number; inconclusive: number; validating: number };
  hypothesis_total: number;
  agent_count: number;
  confidence: number;
  has_conclusion: boolean;
}

export interface StreamHandlers {
  onToken: (text: string) => void;
  onStatus?: (data: { phase: string; message: string }) => void;
  onToolStart?: (data: { tool_name: string; arguments: unknown; agent?: string; agent_name?: string; agent_icon?: string; node_id?: string; discovery?: boolean }) => void;
  onToolResult?: (data: { tool_name: string; duration_ms: number; summary?: string; is_error?: boolean; agent?: string; agent_name?: string; agent_icon?: string; node_id?: string; discovery?: boolean }) => void;
  onApprovalRequired?: (data: { tool_name: string; arguments: unknown }) => void;
  onPhase?: (data: { phase: string; label: string; summary: string | null }) => void;
  onHypothesis?: (data: HypothesisNode) => void;
  onHypothesisStatus?: (data: { id: string; status: string; evidence: string; agent?: string }) => void;
  onConclusion?: (data: InvestigationConclusion) => void;
  onAgents?: (data: { agents: DeepAgent[] }) => void;
  onSaved?: (data: { id: string }) => void;
  onDone?: () => void;
  onError?: (msg: string) => void;
}

/** Stream an agent turn over SSE using fetch + ReadableStream parsing. */
export async function streamMessage(
  chatId: string,
  content: string,
  handlers: StreamHandlers,
  scope?: MessageScope,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/chats/${chatId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, ...(scope ?? {}) }),
    credentials: "include",
    signal,
  });
  await consumeSSE(res, handlers);
}

/** Reconnect to an in-flight turn's SSE stream (replays buffered events first). */
export async function reconnectStream(
  chatId: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/chats/${chatId}/stream`, { credentials: "include", signal });
  await consumeSSE(res, handlers);
}

/** Whether an agent turn is currently running for this chat (server-side). */
export async function isTurnActive(chatId: string): Promise<boolean> {
  try {
    const r = await http<{ active: boolean }>(`/chats/${chatId}/active`);
    return r.active;
  } catch {
    return false;
  }
}

export interface ExecHandlers {
  onStart?: (data: { command: string; destructive: boolean }) => void;
  onStatus?: (text: string) => void;
  onOutput: (text: string, stream: "stdout" | "stderr") => void;
  onApprovalRequired?: (data: { command: string; message: string }) => void;
  onExit?: (data: { code: number | null; duration_ms: number }) => void;
  onError?: (msg: string) => void;
  onDone?: () => void;
}

/** Execute an allowlisted CLI command on the host and stream its output over SSE. */
export async function streamCommand(
  chatId: string,
  command: string,
  handlers: ExecHandlers,
  options?: { confirm?: boolean; signal?: AbortSignal; mode?: "command" | "kql" },
): Promise<void> {
  const res = await fetch(`${API_BASE}/chats/${chatId}/exec/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      command,
      confirm: options?.confirm ?? false,
      mode: options?.mode ?? "command",
    }),
    credentials: "include",
    signal: options?.signal,
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const j = await res.json();
      detail = j.detail ?? detail;
    } catch {
      /* ignore */
    }
    handlers.onError?.(detail);
    return;
  }
  if (!res.body) {
    handlers.onError?.("No response stream");
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        handlers.onDone?.();
        return;
      }
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        if (rawLine.startsWith("event:")) event = rawLine.slice(6).trim();
        else if (rawLine.startsWith("data:")) data += rawLine.slice(5).trim();
      }
      if (!data) continue;
      let parsed: any;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      switch (event) {
        case "exec_start":
          handlers.onStart?.(parsed);
          break;
        case "status":
          handlers.onStatus?.(parsed.text ?? "");
          break;
        case "stdout":
          handlers.onOutput(parsed.text ?? "", "stdout");
          break;
        case "stderr":
          handlers.onOutput(parsed.text ?? "", "stderr");
          break;
        case "approval_required":
          handlers.onApprovalRequired?.(parsed);
          break;
        case "exit":
          handlers.onExit?.(parsed);
          break;
        case "error":
          handlers.onError?.(parsed.message ?? "Command failed");
          break;
        case "done":
          handlers.onDone?.();
          break;
      }
    }
  }
  handlers.onDone?.();
}


async function consumeSSE(res: Response, handlers: StreamHandlers): Promise<void> {
  if (!res.body) {
    handlers.onError?.("No response stream");
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    let value: Uint8Array | undefined;
    let done = false;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      // Aborted (Stop button or navigation) — end gracefully. The backend keeps
      // working in the background; partial/final answer is persisted.
      if ((err as Error)?.name === "AbortError") {
        handlers.onDone?.();
        return;
      }
      throw err;
    }
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. Different servers emit either
    // "\n\n" or "\r\n\r\n", so split on both to avoid buffering the whole stream.
    const frames = buffer.split(/\r\n\r\n|\n\n/);
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const rawLine of frame.split(/\r\n|\n/)) {
        const line = rawLine;
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let parsed: any;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      switch (event) {
        case "token":
          handlers.onToken(parsed.text ?? "");
          break;
        case "status":
          handlers.onStatus?.(parsed);
          break;
        case "tool_start":
          handlers.onToolStart?.(parsed);
          break;
        case "tool_result":
          handlers.onToolResult?.(parsed);
          break;
        case "approval_required":
          handlers.onApprovalRequired?.(parsed);
          break;
        case "phase":
          handlers.onPhase?.(parsed);
          break;
        case "hypothesis":
          handlers.onHypothesis?.(parsed);
          break;
        case "hypothesis_status":
          handlers.onHypothesisStatus?.(parsed);
          break;
        case "conclusion":
          handlers.onConclusion?.(parsed);
          break;
        case "agents":
          handlers.onAgents?.(parsed);
          break;
        case "saved":
          handlers.onSaved?.(parsed);
          break;
        case "done":
          handlers.onDone?.();
          break;
        case "error":
          handlers.onError?.(parsed.message ?? "error");
          break;
      }
    }
  }
}
