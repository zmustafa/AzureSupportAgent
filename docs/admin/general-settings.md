---
layout: default
title: General Settings
parent: Administration
nav_order: 3
description: Configure application behavior, safety, retention, tools, scan limits, thresholds, and feature tuning.
permalink: /admin/general-settings/
---

# General settings

**Permission:** `settings.write`

## Purpose

**App route:** `/admin/settings`
General is the central runtime policy surface. Search the page by setting label and save related changes together. Backend validation clamps numeric values, normalizes lists, and enforces dependent thresholds.

## Prerequisites and data sources



## Tabs and actions

### Setting groups

- **Experience:** custom instructions, response style, token cap, automatic titles, suggestions, progress detail, scope/management-group clarification, and problem proposals.
- **Retention:** chat retention days; Evidence Locker standard/audit retention and default metrics inclusion.
- **Agent and tools:** MCP read-only, Entra MCP, automatic write execution, tool iteration/result/discovery limits, request timeout, built-in tools, network allow/deny lists, and network timeout.
- **Command and sandbox:** command execution/allowlist/timeout; sandbox tools, timeout, and auto-install.
- **Assessment/design:** severity weights, score bands, concurrency, per-check/run budgets, confidence threshold, and architecture category colors.
- **Coverage and posture:** AMBA, telemetry, Backup/DR, identity, RBAC, policy exemptions, Change Explorer, alert analysis, and workload-health tuning.
- **Lifecycle/intelligence:** Telemetry Intelligence timespan/row limit, Performance Profiler window/interval/cap, Quota thresholds/concurrency/zero rows, Reservations horizon/digest routing/schedule/time zone, Radar TTL/lead days/feed, and evidence retention.
- **Automation/discovery:** deep-investigation parallelism and workload Autopilot/nightly refresh behavior.

### Settings inventory

The labels can evolve, but the following saved keys make every current setting searchable.

| Group | Settings |
| --- | --- |
| Experience | `custom_instructions`, `response_style`, `max_tokens`, `auto_title`, `suggestions`, `progress_detail`, `scope_clarification`, `mgmt_group_clarification`, `propose_problems`, `retention_days` |
| Agent runtime | `mcp_read_only`, `entra_mcp_enabled`, `auto_execute_writes`, `max_tool_iterations`, `tool_result_limit`, `tool_discovery_limit`, `request_timeout_seconds` |
| Built-in network tools | `builtin_tools_enabled`, `builtin_tools_disabled`, `network_egress_denylist`, `network_egress_allowlist`, `network_tool_timeout_seconds` |
| Sandbox and commands | `sandbox_tools_enabled`, `sandbox_command_timeout_seconds`, `sandbox_auto_install`, `command_execution_enabled`, `command_allowlist`, `command_timeout_seconds` |
| Assessments and architecture | `assessment_severity_weights`, `assessment_score_good`, `assessment_score_warn`, `assessment_concurrency`, `assessment_check_timeout_s`, `assessment_run_budget_s`, `assessment_confidence_high_pct`, `architecture_category_colors` |
| Monitoring/telemetry/Backup-DR | `amba_cache_ttl_s`, `amba_misconfig_counts_as_gap`, `amba_threshold_tolerance_pct`, `telemetry_cache_ttl_s`, `telemetry_per_resource_scan_cap`, `telemetry_scan_concurrency`, `telemetry_approved_workspaces`, `backupdr_cache_ttl_s`, `backupdr_stale_drill_days`, `backupdr_last_job_sla_hours` |
| Identity and RBAC | `identity_expiry_days`, `identity_cache_ttl_s`, `identity_mfa_scan_cap`, `app_registrations_limit`, `rbac_cache_ttl_s`, `rbac_max_rows`, `rbac_tools_enabled` |
| Policy and changes | `policy_exemption_require_justification`, `policy_exemption_max_expiry_days`, `policy_exemption_block_never_expires`, `changeexplorer_resolve_identities`, `changeexplorer_change_limit` |
| Telemetry intelligence/performance | `teleintel_cache_ttl_s`, `teleintel_default_timespan`, `teleintel_max_rows`, `perfprofile_cache_ttl_s`, `perfprofile_window`, `perfprofile_interval`, `perfprofile_scan_cap` |
| Quota | `quota_cache_ttl_s`, `quota_threshold_watch`, `quota_threshold_warning`, `quota_threshold_critical`, `quota_scan_concurrency`, `quota_hide_zero_usage` |
| Reservations | `reservations_cache_ttl_s`, `reservations_window_days`, `reservations_digest_enabled`, `reservations_digest_recipients`, `reservations_digest_connector_ids`, `reservations_digest_schedule_kind`, `reservations_digest_weekday`, `reservations_digest_time`, `reservations_digest_timezone` |
| Retirement and evidence | `radar_cache_ttl_s`, `radar_digest_lead_days`, `radar_azure_updates_feed_enabled`, `radar_azure_updates_feed_url`, `evidence_retention_standard_days`, `evidence_retention_audit_days`, `evidence_include_metrics_default` |
| Investigation and alerts | `deep_parallel_enabled`, `deep_parallel_count`, `alert_analysis_cache_ttl_s`, `alert_analysis_threshold_tolerance_pct` |
| Workload discovery/health | `autopilot_autosave_confidence`, `autopilot_auto_assess`, `autopilot_auto_architecture`, `workload_health_weights`, `workload_nightly_refresh` |

List editors are normalized and deduplicated. Time zones use IANA names, schedule times use `HH:MM`, durations use ISO-8601 values, and architecture colors use known categories with `#rrggbb` values. Numeric controls are bounded by backend validation; after save, re-open the section to confirm the effective value.

## Freshness and scope behavior



## Workflow overview

### High-impact settings

`mcp_read_only` hides mutation tools. `auto_execute_writes` can bypass the normal approval wait and should remain off unless the organization has an equivalent control. Network allow/deny lists affect built-in egress. Command and sandbox toggles expose execution surfaces. Approved workspaces define telemetry destination drift. Policy exemption controls determine whether perpetual or unjustified exemptions are accepted.

Quota defaults are Watch 70%, Warning 85%, Critical 95%, with enforced ordering. Reservations digest supports daily/weekly recurrence, weekday, UTC-style time value, IANA time zone, recipients, and connector IDs. Values shown in the UI and saved response are authoritative.

### Change procedure

1. Record the reason and current value.
2. Change the smallest related set.
3. Save and review validation-adjusted values.
4. Exercise one representative workflow.
5. Inspect Audit Log; revert if behavior differs from the approved plan.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Troubleshooting index]({{ site.baseurl }}/reference/troubleshooting/)
- [System prompts and scoring]({{ site.baseurl }}/admin/prompts-scoring/)
