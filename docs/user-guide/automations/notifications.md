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

**Permissions:** `notifications.read`, `notifications.manage`

## Purpose

**App route:** `/notifications`
The notification engine normalizes event type, source, severity, title, message, facts, and tenant. Rules match events and fan out to the in-app center or configured connectors. With no rules, the baseline behavior retains events in-app.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### In-app center

The bell shows tenant-scoped notifications and unread count. Filter by severity/type, open an item, mark read/unread, or clear according to the controls shown. The client refreshes periodically and when the page regains visibility; brief delay is normal.

### Routing rules

1. Select the event types and sources to match.
2. Set the minimum severity and any available filters.
3. Choose in-app and enabled connector destinations.
4. Save disabled and use the supported test action.
5. Confirm the real destination and delivery record, then enable.
6. Review outbox/delivery errors after relevant events occur.

Rules and events are tenant-isolated. Delivery to Teams, Slack, email, PagerDuty, SIEM, or automation destinations depends on the exact configured connector type; see [Connectors]({{ site.baseurl }}/connectors/).

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety and limitations and troubleshooting

A successful test proves only the tested path at that time. Avoid routing sensitive telemetry broadly. Use minimum necessary severity and destination scope, but do not filter so aggressively that critical events disappear.

- **No in-app events:** verify `notifications.read`, tenant, filters, and whether producers emitted events.
- **Rule never matches:** compare exact normalized source/type and severity ordering.
- **External delivery fails:** inspect connector enabled/status, secret presence, endpoint policy, and delivery detail.
- **Duplicate delivery:** check overlapping rules and task-level connector routing.

## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

- [Connector overview]({{ site.baseurl }}/connectors/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
