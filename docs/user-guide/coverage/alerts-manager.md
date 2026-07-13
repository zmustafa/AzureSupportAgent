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
- Azure Monitoring Contributor or a narrower custom role for actual rule/action-group writes.
- Configured connectors where a workflow sends to an external ticketing or notification system.

## Tabs and actions

### Tabs

- **Overview** summarizes gaps, overlaps, ineffective/clean rules, activity-log coverage, and reference cost estimates.
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

- An **overlap** means multiple rules monitor similar signals/targets or duplicate a notification path; it is a review signal, not automatically an error.
- An **AMBA gap** means the configured baseline expects a rule that was not matched.
- **Pending** is not deployed; **approved** may still await apply; **applied** should be verified against Azure; **failed** requires error review.
- A successful test notification proves the tested path at that moment, not complete end-to-end incident delivery.

## Workflow overview

### Implementation-grounded usage scenarios

1. **Close selected AMBA gaps:** open `/alerts-manager/gaps`, select supported actionable rows, preview and validate a deployment plan against a live Action Group, submit child changes, then approve and apply them from `/alerts-manager/changes`.
2. **Remove duplicate receiver delivery:** open `/alerts-manager/visualize`, trace duplicated rule-to-Action-Group-to-receiver paths, confirm the overlap in `/alerts-manager/overlaps`, and submit the smallest rule or routing change through the managed ledger.
3. **Recover from an out-of-band conflict:** open `/alerts-manager/changes`, inspect a **Stale** row whose concurrency hash no longer matches Azure, refresh live inventory, create a new request instead of forcing the old payload, and run **Analyze again** after apply.

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

Connections configured for automatic writes may bypass the normal pending step. Use that mode only under an organization-approved control model.

### Reduce noise safely

Start with notification-path visualization, overlap evidence, and firing history. Prefer a narrowly scoped threshold, dimension, window, or evaluation-frequency proposal over disabling a rule. Current Alerts Manager does not implement Alert Processing Rule suppression windows. Test notification routing where supported, document the reason, and monitor incident detection after the change.

## Interpretation of results

Treat overlap, gap, cost, and simulator output as decision support. An overlap may be intentional layered escalation; a gap is relative to the selected AMBA baseline; cost is a reference estimate rather than a bill; and simulated notification edges show configured paths, not guaranteed downstream processing. **Applied** means the request completed, but only refreshed Azure inventory and a new analysis verify convergence.

## Exports, history, scheduling, and integrations

### IaC, approval, and export

Gap and deployment-plan flows produce reviewable change plans for supported rules; rule/action-group editors submit managed changes rather than silently editing Azure. Generated payloads and previews must be checked for scopes, receiver secrets, region support, naming, and cost. Sensitive receiver values must be supplied through the organization's secret-management process, never embedded in documentation or source control.

## Safety and limitations

- Alert-state, rule, and action-group operations are distinct permissions.
- Query validation is best-effort and cannot predict every runtime data pattern or billing impact.
- Closing an alert does not suppress future firings.
- Suppression and dynamic tuning can hide real incidents; keep narrow scope, expiry, ownership, and an audit rationale.
- Portal/IaC changes made outside the app may remain invisible until refresh.
- Inventory collectors can return `partial` or `truncated` metadata; do not treat absent rules, action groups, or paths as proof of absence when either flag is set.
- Never include webhook URLs, tokens, email addresses, tenant IDs, or other live identifiers in exported examples.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Write button is disabled | Check the specific product permission, connection `read_only` state, and capability matrix. |
| Rule validation fails | Verify signal family, target resource type/region, metric namespace, dimensions, query, and evaluation settings. |
| Change remains pending | A user with `alerts_manager.approve` must decide it; inspect the Changes tab. |
| Applied change is absent | Refresh rules, inspect request error/audit fields, and confirm Azure RBAC at the target scope. |
| Duplicate notifications persist | Trace every rule-to-action-group path, including activity-log and externally managed rules. |

## Related pages

- [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)
