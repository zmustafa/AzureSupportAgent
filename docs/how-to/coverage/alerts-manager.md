---
layout: default
title: Operate Alerts Manager
parent: Coverage operations
grand_parent: How-to guides
nav_order: 2
description: Triage alerts and safely author, approve, apply, verify, and roll back Azure Monitor changes.
permalink: /how-to/coverage/alerts-manager/
feature_ids: [PROACTIVE_NAV:alerts-manager, ALERTS_MANAGER_NAV:action-groups, ALERTS_MANAGER_NAV:changes, ALERTS_MANAGER_NAV:deployment-plans, ALERTS_MANAGER_NAV:gaps, ALERTS_MANAGER_NAV:inbox, ALERTS_MANAGER_NAV:manage-rules, ALERTS_MANAGER_NAV:overlaps, ALERTS_MANAGER_NAV:overview, ALERTS_MANAGER_NAV:rules, ALERTS_MANAGER_NAV:visualize]
---

# Operate Alerts Manager

## Prerequisites

- `alerts_manager.read` for analysis and inventory.
- `alerts_manager.alert_state_write` for acknowledge, close, and reopen.
- `alerts_manager.rule_write` for metric, log-query, and Activity Log proposals; `alerts_manager.advanced_rule_write` for Smart Detector and Prometheus proposals.
- `alerts_manager.action_group_write`, `alerts_manager.bulk_write`, `alerts_manager.query_preview`, `alerts_manager.test_notifications`, `alerts_manager.amba_blueprint_write`, `alerts_manager.delete`, or `alerts_manager.approve` for the corresponding task.
- Azure read access for inventory and appropriate Azure Monitor rights for changes.
- A writable connection for managed writes. A read-only connection disables management controls even when the user has product permission.

## Route

Open `/alerts-manager`. It normalizes to `/alerts-manager/overview`. Current routes are **Overview**, **Alert instances**, **Overlaps**, **Gaps**, **Rule analysis**, **Rule management**, **Action groups**, **Deployment plans**, **Visualize**, and **Managed changes**.

> Alert Processing Rules, suppression/maintenance rules, routing-rule catalogs, Templates/GitOps, and legacy Analysis History/Decisions tabs are not current workflows.

## How to refresh analysis and preserve evidence

1. Select the connection and workload, subscription, or management-group scope.
2. Check **Updated**, **stale**, and **cached**. Opening the page can show the prior report.
3. Select **Analyze alerts** or **Analyze again** and monitor the background job.
4. Review Overview, activity-log coverage, overlaps, gaps, Rule analysis, cost estimates, and trend.
5. If the report is marked `partial` or `truncated`, narrow scope or restore collector visibility before using absence as evidence.
6. Export the loaded analysis as CSV, XLSX, or JSON, or select **Evidence** to preserve it.
7. After any managed apply, respond to **Data stale — Analyze again**; the refresh also reconciles the managed-rule inventory.

**Expected result:** A connection/scope-specific report is cached with a generated time and exportable evidence.

**Verification:** Confirm the scope and generated time, then compare post-apply counts only after a new analysis.

## How to triage, acknowledge, close, or reopen an alert

1. Open `/alerts-manager/inbox` and refresh the alert instances.
2. Filter by severity, state, resource, or time and page through the results.
3. Open an instance and inspect fired time, monitor condition, target, and state history.
4. Acknowledge when ownership is established.
5. Close only after resolution or an accepted disposition.
6. Reopen if the disposition was wrong or work must resume.
7. Continue to **Visualize**, **Overlaps**, or **Rule analysis** when the symptom appears recurrent.

**Expected result:** Azure records the requested alert-state transition and the history updates.

**Verification:** Refresh the Inbox and confirm the state and timestamp. Acknowledge/close does not fix the resource, edit the rule, or suppress future firings.

## How to visualize notification paths and separate them from overlaps

1. Open `/alerts-manager/visualize` and run the notification simulation for the selected scope.
2. Trace the rendered resources and rules through Action Groups to receivers; inspect duplicate or missing route edges.
3. Open `/alerts-manager/overlaps` to find rules sharing a signal/target or notification path.
4. Expand a group and compare scopes, conditions, severities, Action Groups, and receiver paths.
5. Decide whether the repeated path is intentional escalation or unintended duplicate delivery; use firing history separately to judge noisy behavior.

**Expected result:** Simulated notification topology and structural overlap evidence are evaluated separately from firing frequency.

**Verification:** Trace each suspected duplicate from rule to Action Group to receiver. An overlap is a review signal, not automatically an error.

## How to add missing AMBA alerts in bulk

1. Open `/alerts-manager/gaps` and filter to supported metric baseline gaps.
2. Select individual rows or all visible actionable rows.
3. Open the remediation drawer and select one healthy live Action Group.
4. Preview proposals. New current proposals are enabled on apply.
5. Review metric name, namespace, aggregation, operator, threshold, dimensions, window, frequency, target, severity, and estimated cost.
6. Resolve blockers. Live metric-definition preflight fails closed when a metric, aggregation, or dimension is unsupported.
7. Validate the plan, include or exclude individual items, then submit.
8. Open the focused Deployment plan or Managed changes and continue through approval and apply.

**Expected result:** Submission creates ordered pending managed changes; it does not change Azure.

**Verification:** Confirm every selected gap has a pending child change and that blocked/equivalent rows were not submitted as creates. Rejected, failed, stale, or applied history does not block a new plan; only active pending/approved changes do.

## How to author or edit a metric rule, including dynamic thresholds

1. Open `/alerts-manager/manage-rules`, refresh, and select **Create rule**, **Edit**, or **Clone**.
2. Choose **Metric** and use the Azure-backed subscription, resource-group, placement-region, and scope selectors.
3. Select the live metric, namespace, supported aggregation, dimensions, window, and evaluation frequency.
4. For a static threshold, enter the operator and numeric value.
5. For an implemented dynamic threshold, choose **Dynamic**, sensitivity (**High**, **Medium**, or **Low**), operator, minimum failing periods, evaluation periods, and ignore-data-before time when needed.
6. Use **Preview last 6h** when `alerts_manager.query_preview` is available.
7. Add Action Groups and choose enabled state.
8. Save. The editor validates, runs the noise guard, and creates a managed change request.

**Expected result:** A pending create/update request contains the validated desired rule and current-state snapshot.

**Verification:** Review the noise-guard findings and managed-change details before approval; after apply, refresh Rule management and re-analyze.

## How to author log, Activity Log, Smart Detector, or Prometheus rules

1. Start **Create rule** and choose the family.
2. For log rules, select a Log Analytics workspace, enter bounded KQL, evaluation settings, optional identity, and run **Validate and preview query**.
3. For Activity Log rules, define exact category/condition and target subscription, then select an Action Group.
4. For Smart Detector or Prometheus, obtain `alerts_manager.advanced_rule_write`, use the family-specific fields, and verify target API/region support.
5. Review cost guidance, scopes, identities, receivers, and enabled state.
6. Save to run validation and noise guard, then inspect the pending change.

**Expected result:** Supported advanced authoring produces a reviewed request, never an immediate silent mutation.

**Verification:** Preview where supported, inspect the resulting ARM body in **Details**, and verify after apply in Azure and refreshed inventory.

## How to tune noise without hiding incidents

1. Begin with **Visualize**, **Overlaps**, firing history, Rule analysis recommendations, and estimated cost.
2. Edit the narrowest rule rather than broadly disabling coverage.
3. Use metric preview or bounded KQL preview to test the candidate condition.
4. Review the editor's noise guard, including actionable overlaps, intentional escalation layers, and projected duplicate receiver deliveries from 30-day history.
5. Prefer a justified threshold, dimensions, evaluation frequency/window, or dynamic-threshold sensitivity change.
6. Submit with a reason, approve through separation of duties, apply, and monitor detection after the change.

**Expected result:** The proposal reduces demonstrated duplication or unstable firing while retaining required signals.

**Verification:** Compare fresh firing history, overlap groups, coverage gaps, and incident outcomes. Current Alerts Manager does not provide Alert Processing Rule suppression windows.

## How to create, edit, clone, enable, delete, or test an Action Group

1. Open `/alerts-manager/action-groups` and select **Refresh**.
2. Inspect enabled state, receiver count, dependencies, and rule usage.
3. Select **Create action group**, **Edit**, or **Clone**. Choose subscription, resource group, placement region, and receiver types.
4. For advanced receivers, use Azure-backed selectors for Functions, Logic Apps, Event Hubs, Automation webhooks, and workspaces where offered.
5. Submit the create/update as a managed request. Enable/disable also follows managed change controls.
6. Before deletion, detach all dependencies; deletion remains disabled while dependency count is nonzero.
7. To test, select **Test**, type `SEND TEST`, and expect real delivery attempts to every configured receiver.

**Expected result:** Authoring produces a pending request; a notification test reports current delivery success or failure.

**Verification:** Refresh inventory after apply. For tests, check each endpoint or mailbox and remember that success proves only the tested moment.

## How to build and submit a deployment plan

1. Open `/alerts-manager/deployment-plans` or arrive from selected gaps.
2. Create a draft from supported gaps or an immutable AMBA blueprint assignment.
3. Select the target subscription, workload, or workload group and one live Action Group.
4. Preview classifications such as create, equivalent, blocked, or invalid.
5. Include/exclude items and validate the draft.
6. Resolve active blockers by opening or cancelling genuine pending/approved child changes, then recheck.
7. Submit. Ordered child changes become pending and the plan opens focused.

**Expected result:** A validated plan becomes a batch of pending managed changes with no Azure write.

**Verification:** Match the plan item count to child changes and inspect each desired rule. An approved plan may still await Apply.

## How to review, approve, reject, or cancel a plan

1. Open the focused plan and inspect source, assignment, Action Group, validations, item classifications, and desired payloads.
2. Approve only a pending plan; provide a review reason.
3. Reject a pending plan when it should not proceed.
4. Cancel an approved-but-unapplied plan by rejecting its remaining pending/approved children.
5. Recreate the plan if approved content must change; do not edit approved payloads in place.

**Expected result:** The plan and child statuses reflect the decision while preserving audit history.

**Verification:** Confirm pending count becomes approved or rejected and no Azure resource changed merely because approval occurred.

## How to approve, reject, apply, and verify managed changes

1. Open `/alerts-manager/changes`; the red pulsing badge reports pending plus approved items across all server-side pages.
2. Open **Details** and compare current Azure state, validated desired configuration, resulting ARM body, method, target, and concurrency hash. Signed URL query strings and secret-bearing fields are redacted.
3. For pending rows, provide a reason and select **Approve** or **Reject**. Bulk decision uses one reason for selected pending rows.
4. For approved rows, select **Apply to Azure**. Bulk apply uses up to six concurrent workers; each row remains independently audited.
5. Watch each row become applied, failed, or stale. Successful siblings are not hidden by failures.
6. Refresh Rule management/Action groups, then select **Data stale — Analyze again**.
7. Verify exact enabled state, condition, scope, and Action Group routing in the refreshed app or Azure.

**Expected result:** Only approved changes are sent to Azure, and terminal state plus evidence/error is retained.

**Verification:** Treat **Applied** as an execution result, then independently confirm live Azure state and fresh analysis convergence.

## How to handle failure, stale state, retry, and rollback

1. For **Failed**, read the error and correct permission, validation, conflict, region, metric, query, or receiver issues before creating a corrected request.
2. Use **Retry clone** only where the row explicitly permits it; the retry restores encrypted source receiver endpoints before applying.
3. For **Stale**, do not force the old payload. Refresh inventory and create a new request because the optimistic-concurrency hash no longer matches Azure.
4. For an applied change, select **Prepare rollback** when `alerts_manager.delete` is available.
5. Review the inverse pending request; rollback is not automatic.
6. Approve and apply the rollback through the same managed flow.
7. Refresh and analyze again to verify restoration.

**Expected result:** Failure history remains intact, and rollback creates a separately approved inverse change linked to the original.

**Verification:** Confirm `rollback of` linkage, applied inverse state, and restored Azure configuration. If Azure changed after the original apply, review the inverse carefully before approval.

## How to perform bulk operations and export analysis

1. In **Rule management**, select up to the bounded set of rules and choose enable, disable, delete, or add Action Group.
2. Enter a reason. Preparation validates all IDs and current snapshots; if any target fails validation, no change rows are created.
3. Review the resulting independent requests in Managed changes, then bulk approve/apply only after inspecting scope and count.
4. Export analysis from the page header as CSV, XLSX, or JSON.
5. Export Activity Log coverage in CSV, JSON, or workbook format when using that section.

**Expected result:** Bulk operations preserve per-rule audit and failure status; exports capture the current analysis.

**Verification:** Compare requested count, created count, selected scope, and post-apply inventory. Current Alerts Manager does not expose rule-definition import; do not describe analysis export as an importable deployment bundle.

## How to diagnose permission, cache, and read-only failures

1. Check the selected connection's **read-only** banner and `/capability` row.
2. Match the action to its exact `alerts_manager.*` permission.
3. Verify Azure RBAC at the target resource, not only the subscription list.
4. Refresh the relevant live inventory when Portal/IaC changes are absent.
5. Re-analyze after apply; cached analysis can otherwise show old gaps, costs, or overlaps.
6. For metric preflight or query preview failures, verify region, provider namespace, dimensions, aggregation, workspace access, and query bounds.

**Expected result:** The UI distinguishes product authorization, connection policy, Azure authorization, stale cache, and validation failures.

**Verification:** Retest the smallest failed operation; do not broaden all permissions as a generic fix.

## Safety and rollback

- Alert-state changes, notification tests, approval, and Azure apply are distinct actions.
- Notification tests are real and may page people or trigger automation.
- Closing an instance never suppresses future firings.
- Dynamic thresholds require sufficient representative history; verify their behavior after deployment.
- Keep receiver secrets out of exports and documentation. The managed ledger encrypts stored payloads and redacts displayed secret-bearing values.
- Rollback is a new pending request and can itself be unsafe if Azure changed afterward.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Write control hidden | Check exact product permission, read-only connection state, and capability. |
| Gap preview blocked | Open/cancel active pending or approved blockers; then recheck metric definitions and Action Group health. |
| Validation fails | Verify family, scope, metric/query, aggregation, dimensions, evaluation settings, identity, and region. |
| Change stays pending | An approver must decide it; approval alone still does not apply it. |
| Apply is stale | Azure changed after the snapshot. Refresh and create a new request. |
| Duplicate notifications remain | Trace all rule-to-Action-Group receiver paths and refresh out-of-band changes. |
| Test reports success but receiver did not process | Inspect the downstream mailbox, endpoint, schema, filtering, and automation logs. |

## Related docs

- [Alerts Manager reference]({{ site.baseurl }}/user-guide/coverage/alerts-manager/)
- [Monitoring Coverage recipes]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/)
- [Change Explorer recipes]({{ site.baseurl }}/how-to/estate-intelligence/change-explorer/)
- [Connection Capability recipes]({{ site.baseurl }}/how-to/coverage/connection-capability/)
