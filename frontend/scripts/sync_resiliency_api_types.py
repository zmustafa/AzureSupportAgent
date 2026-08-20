"""Add the Recovery Readiness types and client to `frontend/src/api.ts`.

A script rather than a direct edit for the same reason `sync_entra_api_types.py` exists: the
editing tool's view of this 14,000-line file drifts from disk and exact-match replacement
fails even on text that `Select-String` and git both confirm is present.

Every replacement is anchored on unique text and verified: a missing or duplicated anchor
aborts the whole run before anything is written, so a partial patch is impossible.
Idempotent — running it twice is a no-op.
"""
from __future__ import annotations

import pathlib
import sys

API = pathlib.Path("src/api.ts")

CLIENT_ANCHOR = """export const api = {
  me: () => http<Me>("/me"),
"""

CLIENT_NEW = """export const api = {
  me: () => http<Me>("/me"),

  // ---------------------------------------------------------------- Recovery Readiness
  /** The vocabulary: scenarios, RTO classes and their wording. Served by the server so the
   *  client never invents a label for `none` or `unknown` — they must not read alike. */
  resiliencyMeta: () => http<ResiliencyMeta>("/resiliency/meta"),
  /** Everything as of the last analysis. Cache-only; never triggers Azure work. */
  resiliencySnapshot: (scope: ResiliencyScope) =>
    http<ResiliencySnapshot>(`/resiliency/snapshot${resiliencyScopeQuery(scope)}`),
  resiliencySummary: (scope: ResiliencyScope) =>
    http<ResiliencySummaryResponse>(`/resiliency/summary${resiliencyScopeQuery(scope)}`),
  resiliencyResources: (
    scope: ResiliencyScope,
    params: { scenario?: string; state?: string; search?: string; offset?: number; limit?: number } = {},
  ) =>
    http<ResiliencyResourcesResponse>(
      `/resiliency/resources${resiliencyScopeQuery(scope, params as Record<string, string | number | undefined>)}`),
  resiliencyResource: (scope: ResiliencyScope, resourceId: string) =>
    http<{ resource: ResiliencyResource; generated_at: string; provenance: Record<string, ResiliencyProvenance> }>(
      `/resiliency/resources/${resourceId.replace(/^\\//, "")}${resiliencyScopeQuery(scope)}`),
  resiliencyBreaches: (scope: ResiliencyScope) =>
    http<ResiliencyBreachesResponse>(`/resiliency/breaches${resiliencyScopeQuery(scope)}`),
  resiliencyWorkloads: (scope: ResiliencyScope) =>
    http<{ report_exists: boolean; rows: ResiliencyWorkload[] }>(
      `/resiliency/workloads${resiliencyScopeQuery(scope)}`),
  resiliencyAnalyzeStart: (scope: ResiliencyScope) =>
    http<{ job: ResiliencyJob | null }>(`/resiliency/analyze/start${resiliencyScopeQuery(scope)}`,
      { method: "POST", body: "{}" }),
  resiliencyAnalyzeJob: (scope: ResiliencyScope) =>
    http<{ job: ResiliencyJob | null }>(`/resiliency/analyze/job${resiliencyScopeQuery(scope)}`),
  resiliencyReference: () => http<ResiliencyReference>("/resiliency/reference"),
  resiliencySaveReference: (body: Partial<ResiliencyReference>) =>
    http<{ reference: ResiliencyReference; rejected: string[] }>("/resiliency/reference",
      { method: "PUT", body: JSON.stringify(body) }),
  resiliencyExportUrl: (scope: ResiliencyScope) =>
    `${API_BASE}/resiliency/export${resiliencyScopeQuery(scope)}`,
"""

TYPES = """
// ==================================================================== Recovery Readiness
/** Recover from WHAT, in HOW LONG, losing HOW MUCH.
 *
 *  Three states in here are routinely confused, and the UI must never render them alike:
 *   - `none`      no recovery path exists for that failure. Worse than slow, not a degree of slow.
 *   - `unknown`   a source could not be read. NOT a claim that the resource is unprotected.
 *   - absent      the scenario does not apply to this resource type. Never a pass. */
export type ResiliencyScenario =
  | "instance_loss" | "zone_loss" | "region_loss" | "data_corruption" | "accidental_delete";

export type ResiliencyRtoClass =
  | "automatic" | "minutes" | "hours" | "day_plus" | "none" | "unknown";

export type ResiliencyRpoState = "known" | "none" | "unknown";
export type ResiliencyConfidence = "high" | "medium" | "low";
export type ResiliencyBreachState = "met" | "breached" | "undetermined" | "not_applicable";

export type ResiliencyScope = {
  workloadId?: string | null;
  subscriptionId?: string | null;
  managementGroupId?: string | null;
  connectionId?: string | null;
};

export function resiliencyScopeQuery(
  scope: ResiliencyScope, extra: Record<string, string | number | boolean | undefined> = {},
): string {
  const params = new URLSearchParams();
  if (scope.workloadId) params.set("workload_id", scope.workloadId);
  if (scope.subscriptionId) params.set("subscription_id", scope.subscriptionId);
  if (scope.managementGroupId) params.set("management_group_id", scope.managementGroupId);
  if (scope.connectionId) params.set("connection_id", scope.connectionId);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export type ResiliencyEvidence = { kind: string; detail: string; source: string };

export type ResiliencyProvenance = {
  source: string; collected_at: string; unreadable: boolean; reason: string; truncated?: boolean;
};

export type ResiliencyVerdict = {
  scenario: ResiliencyScenario;
  /** Only meaningful when `rpo_state === "known"`. */
  rpo_minutes: number | null;
  rpo_state: ResiliencyRpoState;
  rto_class: ResiliencyRtoClass;
  /** A RANGE, never a midpoint, and only ever present alongside `rto_assumptions`. */
  rto_band_minutes: [number, number] | null;
  rto_assumptions: string[];
  basis: ResiliencyEvidence[];
  confidence: ResiliencyConfidence;
  /** False means the scenario cannot happen to this resource type. Render as absent. */
  applicable: boolean;
  target?: { rpo_minutes?: number; rto_class?: ResiliencyRtoClass } | null;
  breach?: { state: ResiliencyBreachState; rpo: boolean; rto: boolean;
             target_rpo_minutes?: number; target_rto_class?: string; reason?: string };
};

export type ResiliencyResource = {
  id: string; name: string; type: string; location: string;
  resource_group: string; subscription_id: string;
  workload_id?: string;
  tier?: string; tier_label?: string; tier_source?: string;
  redundancy: { zones: string[]; zone_redundant: boolean | null; replication: string; sku: string };
  protection: {
    /** THREE states, never a boolean — a boolean cannot express "we did not look". */
    state: "protected" | "not_protected" | "unknown";
    reason: string; policy_name: string; frequency: string;
    retention_days: number | null; recovery_point_age_hours: number | null;
    vault_redundancy: string;
    native_backup: { kind: string; interval_minutes?: number | null; retention_days?: number | null;
                     geo_redundant?: boolean };
  };
  dr: { replicated: boolean; rpo_seconds: number | null; replication_health: string;
        last_test_failover_age_days: number | null };
  advisor: Record<string, unknown>[];
  findings: Record<string, unknown>[];
  size_gb: number | null;
  verdicts: Record<string, ResiliencyVerdict>;
  worst: { rto_class: ResiliencyRtoClass; scenario: string; undetermined: number;
           no_recovery_path: string[] };
  demo_profile?: string;
};

export type ResiliencyScenarioCounts = {
  determined: number; no_recovery_path: number; undetermined: number;
  not_applicable: number; total: number;
};

export type ResiliencySummary = {
  resources: number;
  by_scenario: Record<string, ResiliencyScenarioCounts>;
  protection: Record<string, number>;
  worst: { scenario: string; no_recovery_path: number };
};

export type ResiliencyBreach = {
  resource_id: string; name: string; type: string; scenario: ResiliencyScenario; tier: string;
  rto_class: ResiliencyRtoClass; rpo_minutes: number | null; rpo_state: ResiliencyRpoState;
  target: { rpo_minutes?: number; rto_class?: string } | null;
  no_recovery_path: boolean; total_data_loss: boolean; basis: ResiliencyEvidence[];
};

export type ResiliencyWorkloadScenario = {
  applicable: boolean;
  rto_class: ResiliencyRtoClass;
  rpo_minutes: number | null;
  rpo_state: ResiliencyRpoState;
  weakest_link: { id: string; name: string; type: string; reason: string;
                  shared_platform?: boolean } | null;
  /** The aggregate is computed over DETERMINED components only; the rest are counted here
   *  so a quarter-measured application cannot look fully assessed. */
  coverage: { determined: number; total: number };
  /** Conservative assumptions travel in the payload, not just in UI copy, because an
   *  exported figure has to carry the caveats that qualify it. */
  assumptions: string[];
  no_recovery_path?: { id: string; name: string }[];
};

export type ResiliencyWorkload = {
  workload_id: string; name: string; tier: string; components: number;
  scenarios: Record<string, ResiliencyWorkloadScenario>;
  worst: { scenario: string; rto_class: ResiliencyRtoClass;
           weakest_link: { id: string; name: string } | null };
};

export type ResiliencySnapshot = {
  schema_version: number;
  /** False is the UI's cue to offer the Analyse button rather than render zeros. */
  report_exists: boolean;
  generated_at: string;
  demo: boolean;
  reason: string;
  scope: { scope_kind: string; scope_id: string; subscriptions: string[] };
  summary: ResiliencySummary;
  resources: ResiliencyResource[];
  breaches: ResiliencyBreach[];
  breach_summary: Record<string, number>;
  workloads: ResiliencyWorkload[];
  provenance: Record<string, ResiliencyProvenance>;
  targets_acknowledged?: boolean;
  truncation: Record<string, { exported: number; known_total: number }>;
};

export type ResiliencySummaryResponse = {
  report_exists: boolean; generated_at: string; demo: boolean;
  summary: ResiliencySummary; breach_summary: Record<string, number>;
  provenance: Record<string, ResiliencyProvenance>; targets_acknowledged: boolean;
};

export type ResiliencyResourcesResponse = {
  report_exists: boolean; generated_at: string; total: number;
  rows: ResiliencyResource[]; provenance: Record<string, ResiliencyProvenance>;
};

export type ResiliencyBreachesResponse = {
  report_exists: boolean; rows: ResiliencyBreach[]; summary: Record<string, number>;
  targets_acknowledged: boolean;
};

export type ResiliencyMeta = {
  scenarios: { id: ResiliencyScenario; label: string; description: string;
               redundancy_helps: boolean }[];
  rto_classes: { id: ResiliencyRtoClass; label: string }[];
  rpo_states: string[];
  confidence_levels: string[];
};

export type ResiliencyJob = {
  key: string; status: "running" | "done" | "error"; started_at: string;
  finished_at?: string; error: string;
  messages: { level: string; message: string; at: string }[];
};

export type ResiliencyTier = {
  id: string; label: string;
  scenarios: Record<string, { rto_class: ResiliencyRtoClass; rpo_minutes: number }>;
};

export type ResiliencyReference = {
  version: number; updated_at: string; updated_by: string;
  restore_rates: Record<string, number>;
  mechanism_minutes: Record<string, number>;
  tiers: ResiliencyTier[];
  default_tier: string;
  /** Defaults are usable on screen immediately; an EXPORT that quotes them is refused until
   *  a person has agreed them. */
  targets_acknowledged: boolean;
  targets_acknowledged_by: string;
  targets_acknowledged_at: string;
};
"""


def _read() -> str:
    # newline="" so the file's CRLF endings survive the round trip; rewriting 14,000 lines of
    # line endings would bury the real change in an unreviewable diff.
    with open(API, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(text: str) -> None:
    with open(API, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main() -> int:
    if not API.exists():
        print(f"ABORT: {API} not found — run this from frontend/")
        return 1
    text = original = _read()

    if "resiliencyMeta:" in text:
        print("  already applied: client methods")
    else:
        anchor = CLIENT_ANCHOR.replace("\n", "\r\n") if "\r\n" in text else CLIENT_ANCHOR
        replacement = CLIENT_NEW.replace("\n", "\r\n") if "\r\n" in text else CLIENT_NEW
        found = text.count(anchor)
        if found != 1:
            print(f"ABORT: client anchor matched {found} time(s), expected exactly 1")
            return 1
        text = text.replace(anchor, replacement)
        print("  patched: client methods")

    if "export type ResiliencyVerdict" in text:
        print("  already applied: types")
    else:
        block = TYPES.replace("\n", "\r\n") if "\r\n" in text else TYPES
        text = text.rstrip("\r\n") + ("\r\n" if "\r\n" in text else "\n") + block
        print("  appended: Recovery Readiness types")

    if text == original:
        print("nothing to do")
        return 0
    _write(text)
    print(f"written: {API}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
