---
layout: default
title: Notifications
parent: Automations
grand_parent: User guide
nav_order: 4
description: Use the in-app notification center and route normalized events through governed connector rules.
permalink: /user-guide/automations/notifications/
feature_ids: [AUTOMATIONS_NAV:notifications, ROUTE_ONLY:notifications]
---

# Notifications

**Application route:** `/notifications` and `/automations/notifications`<br>
**Product permissions:** `notifications.read` for the in-app feed and read-state changes; `notifications.manage` for global routing rules

## Purpose

The notification engine normalizes event type, source, severity, title, message, facts, and tenant. Rules match events and fan out to the in-app center or configured connectors. With no rules, the baseline behavior retains events in-app.

## Prerequisites and data sources

- `notifications.read` is required to list tenant-scoped in-app deliveries, read the unread count, and mark one or all notifications read.
- `notifications.manage` is required to list, create, update, or delete routing rules.
- External delivery requires an enabled connector. **Test** is side-effect-free by design; **Send test** creates a real destination event and must follow the provider-specific guide.
- Producer features supply normalized event type, source, severity, title, body, facts, and links.

## Tabs and actions

- **Notifications** lists delivered events, supports unread filtering, and marks one or all items read.
- **Manage rules** lists tenant rules and supports create/update or delete. A rule matches optional event-type and source lists plus a minimum severity, then selects in-app and connector destinations.
- Marking an event read changes local application state only. Saving a rule changes application state only. A later connector delivery creates an external side effect.

## Freshness and scope behavior

- Feed and rule queries are tenant scoped. The feed is bounded to 200 rows even when a larger limit is requested.
- The client refreshes periodically and when the page regains visibility, so an emitted event may not appear immediately.
- In-app events are selected through successful in-app delivery records. Read state is stored on the notification.

## Workflow overview

### In-app center

The bell shows tenant-scoped notifications and unread count. The full page switches between
**All** and **Unread**, filters by source, searches title/body, opens supported source links, and
marks one or all notifications read. There is no mark-unread or delete/clear action in this
surface. The client refreshes periodically, and the bell refreshes when the page regains
visibility; brief delay is normal.

### Routing rules

1. Select the event types and sources to match.
2. Set the minimum severity and any available filters.
3. Choose in-app and enabled connector destinations.
4. Save disabled and use the supported test action.
5. Confirm the real destination and delivery record, then enable.
6. Review outbox/delivery errors after relevant events occur.

Rules and events are tenant-isolated. Delivery to Teams, Slack, email, PagerDuty, SIEM, or automation destinations depends on the exact configured connector type; see [Connectors]({{ site.baseurl }}/connectors/).

## Interpretation of results

- **Unread count** is the number of unread notifications delivered to the in-app channel for the tenant.
- An empty feed means no matching in-app delivery is currently retained; it does not prove that the producer ran or that an external connector succeeded.
- Rule save success proves configuration persistence, not provider delivery. Verify the external artifact at the destination.

## Exports, history, scheduling, and integrations

There is no notification export or dedicated delivery-history screen in this surface. Rules integrate with enabled connectors; provider-side messages, incidents, objects, or queue records remain in the destination after local rule deletion.

## Safety and limitations

A successful test proves only the tested path at that time. Avoid routing sensitive telemetry broadly. Use minimum necessary severity and destination scope, but do not filter so aggressively that critical events disappear.

- Marking read does not delete or retract an event.
- Disabling or deleting a rule stops future matches only. It cannot retract an external delivery.
- Connector **Test** is configuration-only or a lightweight authentication/read probe and intentionally creates no destination record. **Send test** is the separate allowlisted action that performs a real delivery.

## Troubleshooting

| Symptom | Cause and resolution |
| --- | --- |
| Notification bell or route is absent | Assign `notifications.read` to the active role; `notifications.manage` alone governs rules and does not replace the feed read capability. |
| No in-app events appear | Confirm the producer emitted an event, an in-app delivery record exists for the same tenant, and the unread-only filter is not hiding a read item. |
| A rule never matches | Compare the exact normalized event type and source, then confirm the event severity meets the rule's minimum. Empty event/source lists mean any value. |
| An external delivery fails | Confirm the connector is enabled, its secret and endpoint remain valid, and the destination policy permits the operation. Use side-effect-free **Test** first; prepare the destination before another **Send test**. |
| A destination receives duplicates | Check overlapping notification rules and any task-level connector destinations that deliver the same event independently. |
| Unread count changes later than the producer run | Wait for the periodic/visibility refresh, then confirm the producer and in-app delivery completed before treating it as a feed problem. |

## Related pages

- [Connector overview]({{ site.baseurl }}/connectors/)
- [Route and review notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
