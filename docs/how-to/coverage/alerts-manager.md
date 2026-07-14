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

## How to set up Essential Activity Log alerts across a management group

1. Open `/alerts-manager/overview`, select the Azure connection, choose **Management group**, and select the intended management group.
2. Run **Analyze alerts** if the page has no current report. In **Essential Activity Log coverage**, check for `partial` or `truncated` warnings before treating a missing row as a gap.
3. Select **Set up missing alerts**. In **Categories**, choose Service Health, Resource Health, Security, and/or Recommendation. Missing and unhealthy categories are preselected.
4. In **Subscriptions**, search, filter, group, and page through the resolved subscriptions. Select every intended subscription explicitly; unlisted subscriptions are never inferred.
5. In **Conditions & naming**, map every selected subscription to a destination resource group. Existing update/enable operations retain their existing destination.
6. To reuse a name where it already exists, enter **Preferred resource-group name** and select **Use where available**. If a destination does not exist, enable **Create missing resource groups**, provide a default or row-specific location, and select **Copy name** or type an explicit name.
7. Optionally select **Save as connection default** after all rows resolve. This stores the preferred name, default location, and per-subscription mappings in tenant/connection-scoped application state; it does not create Azure resources.
8. Set the rule-name prefix and review category conditions. Service Health requires at least one incident type; Resource Health requires at least one current status. Optional comma-separated filters are de-duplicated and bounded by the server allowlist.
9. In **Routing**, choose only enabled Action Groups with active receivers. For a multi-subscription scope, prefer **Hybrid central + local routing**: select one healthy visible central Action Group, use matching-name or explicit healthy same-subscription overrides where available, and leave the central group as the supported cross-subscription fallback elsewhere.
10. If a subscription requires a local route and has no healthy local group, explicitly enable local Action Group creation, select **Create local clone** for that row, choose a healthy visible clone source, and enter an Azure-safe prefix. The clone is an approval-gated prerequisite, not an immediate Azure write.
11. Treat **Suggest from ownership** as ranking evidence, not an approval. Inspect full destinations for existing groups and verify any **SIEM-capable route?** hint. Use the separate diagnostic-settings flow for Activity Log ingestion.
12. Select **Review plan**. Inspect resource-group prerequisites first, Action Group prerequisites second, and rules third. Confirm every `local`, `cross subscription`, or `planned clone` relationship. Clone preview intentionally shows IDs and receiver counts without exposing endpoints or secrets.
13. Select **Validate**. If inputs or live inventory changed, rebuild the preview. Submit only after validation passes.
14. Optionally save the resolved resource-group and Action Group preferences as the connection default. This writes tenant-and-connection-scoped application state and performs no Azure write.
15. Select **Submit pending changes**. The result is an ordered batch of pending application records; no Azure write occurs.

**Expected result:** Missing resource groups become pending prerequisites, explicitly selected local clones become pending Action Group prerequisites, and actionable Activity Log rule creates/updates/enables follow them. Equivalent, blocked, and invalid rows are not submitted as Azure changes.

**Verification:** Open `/alerts-manager/changes`, filter to **Action Required**, and compare the batch order, target subscription, destination resource group, clone source/target IDs, prerequisite linkage, routing relationship, category, and sanitized ARM details with the reviewed preview.

## How to approve and bulk-apply Activity Log prerequisites and rules

1. In `/alerts-manager/changes`, select the pending rows from the reviewed Activity Log batch.
2. Open **Details** for representative and high-risk rows. Confirm resource-group create/PUT requests, high-risk Action Group clone requests, and rule requests target the intended subscription, retained or mapped group, conditions, and routes. Secret-bearing receiver fields are redacted in this view.
3. Select a pending rule or clone. Wait while the backend expands its transitive prerequisites, including prerequisites on other server-side pages, and review the **requested**, **prerequisites added**, and total counts. Use **Select all** only when the intention is to load every actionable page and resolve the combined closure.
4. Approve or reject pending rows with a reason. Bulk approval includes pending prerequisites returned by the backend. To cancel an already approved but unapplied row, use its **Reject** control or include it in a bulk **Reject** decision. Approval and rejection change only application state and audit history.
5. Select the approved closure and choose **Apply to Azure**.
6. Confirm the prompt. The backend recomputes dependencies and applies topologically: resource group, then Action Group, then dependent rule. A failed branch skips only its descendants; independent branches continue.
7. A row-level **Apply to Azure** request also uses the dependency-aware bulk-apply endpoint with that row as the requested selection. It expands and applies approved prerequisites in topological order; it does not require manually applying each ancestor first.
8. If a branch fails, review the grouped prerequisite error and affected descendant IDs. Correct or retry the failed prerequisite; skipped descendants remain approved. Select the failed root or an approved descendant and apply again so the server recomputes the closure, retains already-applied ancestors, and resumes eligible descendants.
9. Return to Overview, select **Data stale — Analyze again**, and refresh Essential Activity Log coverage.
10. Verify the created resource groups, enabled rules, exact Activity Log conditions, subscription scopes, and Action Group routes in live inventory or Azure.

**Expected result:** Approved prerequisites and then rules are written to Azure, each applied row receives evidence, and failed siblings remain visible without hiding successful operations.

**Verification:** Confirm every intended category reports covered after a fresh analysis. For any failed/stale row, compare its error and current Azure state rather than reapplying the old payload blindly.

## How to recover or roll back an Essential Activity Log batch

1. For a failed resource-group create, correct location, Azure authorization, or name conflict and build a new wizard preview. Do not make the dependent rule bypass the prerequisite.
2. For a stale Activity Log update, refresh coverage and submit a new request from live state; the old concurrency hash cannot be forced.
3. For an applied Activity Log rule, select **Prepare rollback** with `alerts_manager.delete`, review the inverse pending request, then approve and apply it through the normal flow.
4. For a wizard-created clone, detach every dependent rule before preparing rollback. Dependency checks run again at apply time and block deletion if a reference reappears.
5. Do not expect **Prepare rollback** for a resource-group prerequisite. Automatic deletion is blocked because the group may contain unrelated resources.
6. If a newly created resource group is genuinely unused, inspect its contents and dependencies in Azure and use a separately authorized, reviewed removal process.
7. Run **Analyze again** and verify that the intended prior rule state is restored without reopening a required coverage gap.

**Expected result:** Supported rule rollback is a separately audited pending change; unsafe automatic resource-group deletion never occurs.

**Verification:** Confirm the rollback linkage and fresh Azure rule state. If removal of a prerequisite was separately approved, verify that no unrelated resources were deleted.

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
4. The plan detail currently exposes whole-plan **Approve** and **Reject** only while the plan is pending. To cancel an approved-but-unapplied plan in the UI, open `/alerts-manager/changes`, select its remaining approved children, review the dependency closure, and use bulk **Reject**. The backend plan-decision contract also accepts rejection of an approved plan, but the current plan-detail view does not expose that control.
5. Recreate the plan if approved content must change; do not edit approved payloads in place.

**Expected result:** The plan and child statuses reflect the decision while preserving audit history.

**Verification:** Confirm pending count becomes approved or rejected and no Azure resource changed merely because approval occurred.

## How to approve, reject, apply, and verify managed changes

1. Open `/alerts-manager/changes`; the red pulsing badge reports pending plus approved items across all server-side pages.
2. Open **Details** and compare current Azure state, validated desired configuration, resulting ARM body, method, target, and concurrency hash. Signed URL query strings and secret-bearing fields are redacted.
3. Select any actionable rows and wait for `POST /api/alerts-manager/changes/resolve-dependencies` to expand transitive prerequisites. Review requested versus added prerequisite counts and resolve any missing, cross-connection, type, or cycle error.
4. For pending rows, provide a reason and select **Approve** or **Reject**. For one approved-but-unapplied row, use its row-level **Reject** control. For multiple pending and/or approved-but-unapplied rows, select them and use bulk **Reject**. `POST /api/alerts-manager/changes/bulk-decision` uses one reason, resolves the branch, rejects dependents before prerequisites, and retains prerequisites shared by unselected active dependents.
5. For approved rows, select **Apply to Azure**. `POST /api/alerts-manager/changes/bulk-apply` recomputes the closure and executes its topological order, including RG → Action Group → rule where those dependencies exist.
6. Watch each row become applied, already applied, failed, or skipped. A failed branch skips its descendants while independent branches continue; errors are grouped by failed prerequisite and list affected descendants.
7. Refresh Rule management/Action groups, then select **Data stale — Analyze again**.
8. Verify exact enabled state, condition, scope, and Action Group routing in the refreshed app or Azure.

**Expected result:** Only approved changes are sent to Azure, and terminal state plus evidence/error is retained.

**Verification:** Treat **Applied** as an execution result, then independently confirm live Azure state and fresh analysis convergence.

## How to cancel approved changes individually or in bulk

1. Open `/alerts-manager/changes`, choose **Action Required**, and identify rows that are `approved` but have not been applied.
2. For one row, open **Details**, verify the target and desired ARM body, select **Reject**, and enter a cancellation reason. This calls the individual decision endpoint and changes only the ledger and audit history.
3. For a dependency branch, select the approved dependent row and wait for the server to add its transitive prerequisites. Review the **requested**, **prerequisites added**, and total counts before continuing.
4. For several branches or every actionable row, select the intended rows or use **Select all**. **Select all** fetches all server-side pages before dependency resolution; it is not limited to the visible 100-row page.
5. Select bulk **Reject** and enter one reason. The backend processes the closure in reverse topological order so dependents are rejected before their prerequisites.
6. If the result reports `shared_prerequisite`, leave that prerequisite active: an unselected pending or approved dependent still requires it. Either keep the shared prerequisite or separately review and select every active dependent before retrying.
7. Refresh **Action Required** and verify the cancelled rows are absent there and visible as `rejected` under **Archived** or **All**.

**Expected result:** Pending and approved-but-unapplied selections become rejected without an Azure call; shared prerequisites needed by unselected active branches are retained.

**Verification:** Confirm the decision reason and rejected status in **All** or **Archived**, confirm the actionable badge/count decreased, and refresh Azure inventory only to verify that no Azure resource changed. If a row is already `applied`, stop: it cannot be rejected and requires **Prepare rollback** where supported.

## How to handle failure, stale state, retry, and rollback

1. For **Failed**, read the error and correct permission, validation, conflict, region, metric, query, or receiver issues before creating a corrected request.
2. Use **Retry clone** only where the row explicitly permits it; the retry restores encrypted source receiver endpoints before applying.
3. After a failed prerequisite, leave skipped descendants approved. Retry or correct the root, then select the root or a skipped descendant and apply again. Dependency resolution retains already-applied ancestors and resumes the approved descendants; it does not require reapproval of skipped rows.
4. For **Stale**, do not force the old payload. Refresh inventory and create a new request because the optimistic-concurrency hash no longer matches Azure.
5. For an applied change, select **Prepare rollback** when `alerts_manager.delete` is available.
6. Review the inverse pending request; rollback is not automatic.
7. Approve and apply the rollback through the same managed flow.
8. Refresh and analyze again to verify restoration.

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
- Reject/cancel is available only before apply and requires `alerts_manager.approve`; applied rows cross the cancellation boundary and require `alerts_manager.delete` to prepare a supported rollback.
- Essential Activity Log destination defaults are local tenant/connection configuration. Saving them is not an Azure change and preview always revalidates the mapped groups.
- Activity Log resource-group prerequisites cannot be automatically rolled back.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Write control hidden | Check exact product permission, read-only connection state, and capability. |
| Gap preview blocked | Open/cancel active pending or approved blockers; then recheck metric definitions and Action Group health. |
| Validation fails | Verify family, scope, metric/query, aggregation, dimensions, evaluation settings, identity, and region. |
| Change stays pending | An approver must decide it; approval alone still does not apply it. |
| Reject is unavailable on a change | The row is not pending or approved, or `alerts_manager.approve` is missing. Applied rows cannot be cancelled; use **Prepare rollback** with `alerts_manager.delete` when that target supports rollback. |
| Apply is stale | Azure changed after the snapshot. Refresh and create a new request. |
| Duplicate notifications remain | Trace all rule-to-Action-Group receiver paths and refresh out-of-band changes. |
| Test reports success but receiver did not process | Inspect the downstream mailbox, endpoint, schema, filtering, and automation logs. |
| Destination mapping stays unresolved | Select an existing group, or enable missing-group creation and provide a valid location for the proposed group. |
| Local Action Group override fails preview | Choose a healthy group in the rule subscription, clear the override to use the healthy central fallback, or explicitly plan a local clone. |
| Hybrid routing leaves subscriptions unresolved | Select a healthy visible central Action Group, a healthy local override, or an explicitly enabled clone with a healthy source and safe prefix. |
| Planned clone is invalid | Resolve its resource group/location, enable clone creation, select a visible enabled source with an active receiver, and use an Azure-safe prefix. |
| Approved Activity Log rule returns a prerequisite conflict | Select the rule and use **Apply to Azure**; row-level apply resolves its closure and enforces resource group → Action Group → rule order. Approve any pending ancestor and correct missing, cross-connection, type, or cycle errors first. |
| Selecting a row adds prerequisites not visible on the current page | Review the requested/prerequisite counts. This is the server-authoritative transitive closure, including rows discovered across pages. |
| Bulk approval includes more rows than originally checked | Pending prerequisites are intentionally included. Inspect the expanded closure before confirming; approval remains an application-state write and does not mutate Azure. |
| Bulk apply reports failed and skipped rows together | Use the grouped prerequisite error to correct or retry the failed root. Independent branches already continued, and skipped descendants remain approved for the next closure retry. |
| Rejection reports a shared prerequisite | An unselected active dependent still needs that row. Review the dependent; the backend intentionally leaves the shared prerequisite pending or approved. |
| Clone details omit receiver endpoints | This is the secret-safe design. Preview and audit details expose IDs and counts; encrypted source values are restored only for apply or eligible retry. |
| Prepare rollback is unavailable for the resource group | Automatic group deletion is intentionally blocked; inspect the group and use a separately reviewed Azure removal process only if it is empty and unshared. |
| Wizard-created clone rollback is blocked | Detach all dependent alert rules and refresh. Deletion is guarded both when rollback is prepared and when it is applied. |

## Related docs

- [Alerts Manager reference]({{ site.baseurl }}/user-guide/coverage/alerts-manager/)
- [Monitoring Coverage recipes]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/)
- [Change Explorer recipes]({{ site.baseurl }}/how-to/estate-intelligence/change-explorer/)
- [Connection Capability recipes]({{ site.baseurl }}/how-to/coverage/connection-capability/)
