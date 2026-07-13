---
layout: default
title: Alerts Manager
parent: Coverage
grand_parent: User guide
nav_order: 2
description: Triage alert instances and govern alert rules, action groups, AMBA gaps, and approval-gated changes.
permalink: /user-guide/coverage/alerts-manager/
feature_ids: [PROACTIVE_NAV:alerts-manager, ALERTS_MANAGER_NAV:action-groups, ALERTS_MANAGER_NAV:changes, ALERTS_MANAGER_NAV:deployment-plans, ALERTS_MANAGER_NAV:gaps, ALERTS_MANAGER_NAV:inbox, ALERTS_MANAGER_NAV:manage-rules, ALERTS_MANAGER_NAV:overlaps, ALERTS_MANAGER_NAV:overview, ALERTS_MANAGER_NAV:rules, ALERTS_MANAGER_NAV:visualize]
---

# Alerts Manager

**Product permissions:** `alerts_manager.read`; mutations and privileged previews use `alerts_manager.alert_state_write`, `alerts_manager.action_group_write`, `alerts_manager.rule_write`, `alerts_manager.advanced_rule_write`, `alerts_manager.bulk_write`, `alerts_manager.amba_blueprint_write`, `alerts_manager.query_preview`, `alerts_manager.test_notifications`, `alerts_manager.delete`, and `alerts_manager.approve` according to the action.

## Purpose

**App routes:** `/alerts-manager` and `/alerts-manager/:tab`
Alerts Manager combines current alert operations with rule authoring and governed changes. Unlike the read-only coverage score, some actions can mutate Azure. Availability depends on both the user's permission and the connection's read-only/write policy.

## Prerequisites and data sources

### Prerequisites

- An Azure connection with access to Alert Management, Azure Monitor rules, and action groups in the selected scope.
- `alerts_manager.alert_state_write` to acknowledge or close alert instances.
- `alerts_manager.rule_write`, `alerts_manager.action_group_write`, or the corresponding advanced/bulk permission to propose or execute those changes.
- `alerts_manager.approve` to approve pending requests; notification tests require their dedicated permission.
- Azure access sufficient to read subscriptions, Resource Graph inventories, Activity Log alert rules, Action Groups, and destination resource groups. The selected connection also needs the target-specific ARM write access used at apply time.
- Configured connectors where a workflow sends to an external ticketing or notification system.

## Tabs and actions

### Tabs

- **Overview** summarizes gaps, overlaps, ineffective/clean rules, activity-log coverage, and reference cost estimates.
- **Overview** also hosts **Essential Activity Log coverage** and its five-step setup wizard: Categories, Subscriptions, Conditions & naming, Routing, and Review.
- **Alert instances** lists fired alerts and state history; permitted users can acknowledge or close an instance.
- **Visualize** runs the notification-path simulator and renders resources/rules through action groups to receivers so duplicate and missing routes can be inspected.
- **Overlaps** shows rules monitoring the same signal/target and their notification impact.
- **Gaps** shows missing, disabled, or ineffective baseline coverage and can create reviewed rules or deployment plans for supported gaps.
- **Rule analysis** evaluates observed conditions, targets, action groups, firings, status, recommendations, and estimated cost.
- **Rule management** is the live Azure inventory/editor for supported metric, log, activity, smart-detection, and Prometheus rule families.
- **Action groups** inventories and manages receivers, dependencies, enablement, clone/test, and reviewed deletion where capabilities permit.
- **Deployment plans** groups selected supported gaps into previewable remediation plans.
- **Managed changes** shows pending, approved, rejected, failed, applied, and rollback-capable requests with before/after detail.

Tab visibility can be permission- or capability-dependent. A read-only connection disables write controls even when the signed-in user has a write permission.

## Freshness and scope behavior

### Freshness and interpretation

The Inbox is queried from Azure when fetched. Rules and action groups may be cached and are invalidated after mutations; use Refresh to reconcile out-of-band Portal or IaC changes. Activity-log coverage runs separately and can report progress for a longer collection.

The live Alerts Manager inventory cache lasts 20 seconds, is bounded to 128 entries, and is keyed by application tenant, Azure tenant, connection, scope, and query dimensions. Action Group and rule collectors cap Resource Graph results at 10,000 rows; fired-alert collection caps them at 5,000. A collector that reaches its source limit reports `partial` and `truncated`. The setup wizard does not select metadata-only or unlisted subscriptions automatically.

- An **overlap** means multiple rules monitor similar signals/targets or duplicate a notification path; it is a review signal, not automatically an error.
- An **AMBA gap** means the configured baseline expects a rule that was not matched.
- **Pending** is not deployed; **approved** may still await apply; **applied** should be verified against Azure; **failed** requires error review.
- A successful test notification proves the tested path at that moment, not complete end-to-end incident delivery.

## Workflow overview

### Implementation-grounded usage scenarios

1. **Close selected AMBA gaps:** open `/alerts-manager/gaps`, select supported actionable rows, preview and validate a deployment plan against a live Action Group, submit child changes, then approve and apply them from `/alerts-manager/changes`.
2. **Remove duplicate receiver delivery:** open `/alerts-manager/visualize`, trace duplicated rule-to-Action-Group-to-receiver paths, confirm the overlap in `/alerts-manager/overlaps`, and submit the smallest rule or routing change through the managed ledger.
3. **Recover from an out-of-band conflict:** open `/alerts-manager/changes`, inspect a **Stale** row whose concurrency hash no longer matches Azure, refresh live inventory, create a new request instead of forcing the old payload, and run **Analyze again** after apply.
4. **Establish management-group Activity Log coverage:** select a management group, map every selected subscription to its own existing monitoring resource group or an explicitly approved resource-group prerequisite, choose a healthy visible central Action Group with recommended same-subscription overrides or approved local clones, review server-classified operations, validate, submit pending changes, and apply prerequisites before dependent rules.

### Essential Activity Log destination model

The setup wizard manages four subscription-level categories: **Service Health**, **Resource Health**, **Security**, and **Recommendation**. It creates or repairs Activity Log alert rules; it does not silently configure SIEM ingestion. Security event export requires the separate diagnostic-settings workflow.

For a management-group scope, destination mapping is per subscription:

- A common resource-group name is optional. Each selected subscription can map to a different resource group.
- Existing rule updates and enables retain the existing rule's resource group.
- New rules require a mapped resource group. The wizard verifies each target with ARM before it permits submission.
- **Preferred resource-group name** applies only where a matching group exists. With **Create missing resource groups** enabled, **Copy name** can propose that name for missing groups, but a location is also required.
- If exactly one resource group is visible for a subscription, it is the fallback after saved and preferred mappings. Ambiguous subscriptions remain unresolved rather than receiving an arbitrary destination.
- **Save as connection default** writes tenant-and-connection-scoped application configuration containing the preferred name, default location, and normalized subscription-to-resource-group map. It requires `alerts_manager.rule_write`, is audited, and does not write Azure.

Only enabled Action Groups with at least one active receiver are selectable. **One common Action Group** can be a healthy group visible through the selected connection in another subscription; preview labels each rule relationship `local` or `cross_subscription`. **Hybrid central + local routing** is the recommended multi-subscription model: a same-subscription healthy override wins, otherwise the healthy central Action Group is the fallback. Saved preferred and per-subscription Action Group mappings are tenant-and-connection scoped application configuration, are re-matched only to healthy visible groups, and require `alerts_manager.rule_write` to read or update.

For subscriptions that need a local route but have no healthy override, an operator can explicitly enable local Action Group creation, select the subscriptions, choose a healthy visible clone source, and provide an Azure-safe prefix. The generated clone name appends the first eight subscription-ID characters and is bounded to the Azure name limit. Preview classifies each route as `local`, `cross_subscription`, or `planned_clone`; it shows source/target resource IDs and receiver counts, but never receiver endpoints or secrets. Clone creation is a separate high-risk, approval-gated Action Group prerequisite. Its encrypted payload preserves source receiver configuration for apply and retry. Ownership suggestions rank routes, but the displayed SIEM capability is heuristic and must be verified.

The server preview classifies each rule operation as `create`, `update`, `enable`, `equivalent`, `blocked`, or `invalid`. A pending or approved change for the same target blocks a second proposal. The preview also lists existing rules, duplicate/overlap evidence, full reviewed routing, mandatory and optional conditions, and a direct rule-cost classification of free. That classification excludes downstream ingestion, notification, and operational costs.

Missing resource groups are represented as separate `resource_group` create requests. Planned local clones follow as `action_group` create requests, and dependent `activity_rule` requests follow those in the same batch. Submission sets every row to pending with `auto_apply` disabled and performs no Azure write. Apply checks tenant, connection, expected prerequisite type, and applied status: a clone cannot bypass its resource-group prerequisite, and a rule cannot bypass either its resource-group or planned-clone prerequisite. Bulk apply runs resource groups serially first, Action Groups serially second, and only then uses at most six concurrent workers for the remaining independently audited rows.

Approval and apply remain separate. `alerts_manager.rule_write` builds, previews, validates, submits, and saves destination defaults; `alerts_manager.approve` decides, applies, and retries an eligible failed clone; `alerts_manager.delete` prepares supported rollbacks. Apply creates evidence and invalidates affected inventories. Optimistic concurrency marks an out-of-date rule update `stale`. Resource-group prerequisite creation has no automatic rollback because deleting a group could delete unrelated resources. A wizard-created clone can produce a rollback request only while it has no rule dependencies; dependency discovery blocks deletion both when requested and again at apply time.

### Operational workflows

### Triage an alert

1. Select the intended connection and scope, then refresh the Inbox.
2. Filter by severity, state, resource, or time.
3. Open an alert and inspect fired time, monitor condition, target, and history.
4. Acknowledge only when ownership is clear; close only after resolution or accepted disposition.
5. Use Rule analysis and firing history for recurrence; use Visualize separately to trace its notification route and detect duplicate deliveries.

State changes affect the Azure alert instance; they do not fix the monitored condition or modify the rule.

### Propose a rule or routing change

1. Open **Gaps**, **Rule management**, **Action groups**, or **Deployment plans**.
2. Select an existing object or start a supported authoring flow.
3. Validate metric names, dimensions, query syntax, scopes, thresholds, receivers, and estimated behavior.
4. Preview the before/after deployment plan.
5. Submit the change. In approval-gated mode it remains pending.
6. An approver reviews the diff and approves or rejects it.
7. Confirm applied/failed status and re-query Azure. A failed request retains error details for correction.

Alerts Manager requests in this workflow always enter the managed ledger with automatic apply disabled. Submission and approval do not mutate Azure; only **Apply to Azure** does.

### Reduce noise safely

Start with notification-path visualization, overlap evidence, and firing history. Prefer a narrowly scoped threshold, dimension, window, or evaluation-frequency proposal over disabling a rule. Current Alerts Manager does not implement Alert Processing Rule suppression windows. Test notification routing where supported, document the reason, and monitor incident detection after the change.

## Interpretation of results

Treat overlap, gap, cost, and simulator output as decision support. An overlap may be intentional layered escalation; a gap is relative to the selected AMBA baseline; cost is a reference estimate rather than a bill; and simulated notification edges show configured paths, not guaranteed downstream processing. **Applied** means the request completed, but only refreshed Azure inventory and a new analysis verify convergence.

## Exports, history, scheduling, and integrations

### IaC, approval, and export

Gap and deployment-plan flows produce reviewable change plans for supported rules; rule/action-group editors submit managed changes rather than silently editing Azure. Generated payloads and previews must be checked for scopes, receiver secrets, region support, naming, and cost. Sensitive receiver values must be supplied through the organization's secret-management process, never embedded in documentation or source control.

Analysis exports support CSV, XLSX, and JSON. Essential Activity Log coverage exports support CSV, JSON, and XLSX at the API; the current wizard exposes CSV and JSON controls. Export is read-only and audited. Managed apply creates an Evidence Locker snapshot; the destination-default record is local application configuration rather than an Azure artifact.

## Safety and limitations

- Alert-state, rule, and action-group operations are distinct permissions.
- Query validation is best-effort and cannot predict every runtime data pattern or billing impact.
- Closing an alert does not suppress future firings.
- Suppression and dynamic tuning can hide real incidents; keep narrow scope, expiry, ownership, and an audit rationale.
- Portal/IaC changes made outside the app may remain invisible until refresh.
- Inventory collectors can return `partial` or `truncated` metadata; do not treat absent rules, action groups, or paths as proof of absence when either flag is set.
- A management-group plan changes only explicitly selected subscriptions returned inside the resolved scope. Unlisted subscriptions are not inferred or changed.
- Management-group discovery supports a healthy visible central Action Group across subscriptions, preferred and per-subscription saved mappings, same-subscription local overrides, and explicitly selected approval-gated local clones. A local override must belong to the rule subscription; central fallback is the supported cross-subscription relationship.
- Clone target availability and source health are rechecked during preview. A planned clone also requires an explicitly enabled creation option, a destination resource group, and a valid safe prefix.
- A saved destination policy is a convenience default, not proof that a resource group still exists or that the connection can write it; preview rechecks ARM.
- Automatic resource-group rollback is intentionally unsupported. Clone rollback is also blocked while the clone has dependencies. Remove an unused prerequisite manually only after proving that it contains no unrelated resources or references.
- Never include webhook URLs, tokens, email addresses, tenant IDs, or other live identifiers in exported examples.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Write button is disabled | Check the specific product permission, connection `read_only` state, and capability matrix. |
| Rule validation fails | Verify signal family, target resource type/region, metric namespace, dimensions, query, and evaluation settings. |
| Change remains pending | A user with `alerts_manager.approve` must decide it; inspect the Changes tab. |
| Applied change is absent | Refresh rules, inspect request error/audit fields, and confirm Azure RBAC at the target scope. |
| Duplicate notifications persist | Trace every rule-to-action-group path, including activity-log and externally managed rules. |
| A management-group subscription remains unresolved | Select an existing resource group for that subscription, or enable explicit resource-group creation and provide a valid location. A preferred name is not used when it does not exist unless creation is enabled. |
| A local override is outside the rule subscription | Select a healthy Action Group from that subscription, clear the override to use the healthy central fallback, or explicitly plan a local clone. Cross-subscription routing is supported only for the common/central relationship. |
| A subscription is unresolved in hybrid routing | Select a healthy central Action Group, choose a healthy same-subscription override, or explicitly enable clone creation and select that subscription with a healthy source and valid prefix. |
| Planned clone preview is invalid | Confirm the source is visible, enabled, and has an active receiver; enable local Action Group creation; provide an Azure-safe prefix; and resolve the destination resource group and location. |
| Activity Log rule or clone apply is blocked after approval | Apply linked resource-group prerequisites first, then Action Group prerequisites, then rules. Bulk apply enforces these tiers; individual apply does not bypass tenant, connection, type, or status guards. |
| Clone receiver endpoints appear absent from preview | This is intentional. Preview and audit summaries expose only source/target IDs and receiver counts; encrypted source configuration is restored only for apply or an eligible retry. |
| Resource-group prerequisite cannot be rolled back | This is an intentional safety boundary. Verify whether the group is empty and remove it through an independently reviewed Azure process if appropriate. |
| Wizard-created clone rollback is blocked | Detach every dependent Azure alert rule and refresh inventory. The service checks dependencies before preparing deletion and again before apply. |

## Related pages

- [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)
