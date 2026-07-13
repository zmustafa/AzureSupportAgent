---
layout: default
title: Route and review notifications
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 64
description: Create routing rules, verify external delivery, and operate the in-app notification center.
permalink: /how-to/automations-connectors/notifications/
feature_ids: [AUTOMATIONS_NAV:notifications, ROUTE_ONLY:notifications]
---

# Route and review notifications

## Prerequisites

- `notifications.manage`.
- At least one enabled, provider-verified connector for external delivery.
- A known producer event type and source.
- An authenticated user in the intended tenant.

## Route

- Open `/automations/notifications`.

## How to create and verify a notification rule

1. Select **New rule** and give the rule a purpose-specific name.
2. Leave event types or sources empty only when the rule should match any value; otherwise select exact values.
3. Choose minimum severity: info, warning, error, or critical.
4. Keep **In-app center** selected unless the workflow intentionally excludes it.
5. Select enabled external connectors and save the rule disabled when staged rollout is required.
6. Verify the connector independently: use **Test**, and use **Send test** only when that connector type supports it.
7. Enable the rule and produce a controlled event from a workbook, playbook, or scheduled task.
8. Confirm the event in-app and at each intended external destination.

**Expected result:** Matching events are delivered to the union of selected in-app and connector targets. With no rules, events still appear in the in-app center by default.

**Verification:** Compare the emitted event's exact type, source, and severity with the rule. Confirm the provider artifact rather than relying only on connector status.

## How to review and clear in-app notifications

1. Open **Notifications** from the navigation or the bell.
2. Switch between **All** and **Unread**, filter by source, or search title/body text.
3. Select a notification to mark it read and open its source when a deep link is available.
4. Use **Mark all read** when all currently unread items have been reviewed.
5. Select **Manage rules** to adjust future routing.

**Expected result:** Read state and unread count update, and supported items open their originating run or report.

**Verification:** Refresh or wait for the periodic update and confirm the unread count remains correct.

## Safety and rollback

Marking read does not delete the event or reverse external delivery. There is no need to recreate a notification solely to restore unread state; use source history as the evidence record.

Start with narrow event and severity filters and a non-production destination. Disable or delete the rule to stop future routing; provider-side messages, incidents, or records already created must be handled at the provider.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| New event is delayed | the list refreshes periodically; refresh after confirming the producer completed. |
| Deep link unavailable | use source and title to locate the originating feature history. |
| Wrong tenant's event expected | notifications and rules are tenant-scoped. |
| Rule does not match | verify exact type/source and that event severity meets the threshold. |
| Connector absent from the form | enable and save it first. |
| Duplicate delivery | inspect overlapping rules and task-level connector destinations. |
| External failure | follow the connector's provider-specific guide and recheck endpoint policy and permissions. |
| [Notifications overview]({{ site.baseurl }}/user-guide/automations/notifications/) | Review connector configuration and retry. |
| [Manage connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Scheduled tasks]({{ site.baseurl }}/how-to/automations-connectors/scheduled-tasks/)
- [Workbooks]({{ site.baseurl }}/how-to/automations-connectors/workbooks/)
