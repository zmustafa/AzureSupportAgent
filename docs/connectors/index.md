---
layout: default
title: Connectors
nav_order: 21
description: Configure implemented messaging, ticketing, monitoring, SIEM, security, queue, storage, and automation destinations.
permalink: /connectors/
feature_ids: [ADMIN_NAV:connectors]
has_children: true
---

# Connectors

**App route:** `/admin/connectors`
**Permission:** `connectors.manage`

Connector definitions are encrypted at rest. Secret fields are masked on read; entering a blank secret during edit keeps the existing value. Each connector has type, mode, name, enabled/disabled state, health status, and type-specific fields.

The source registers these exact types: **Teams, Outlook, Email, Jira, ServiceNow, Grafana, Slack, Webhook, PagerDuty, Splunk, Cortex XSOAR, Amazon SQS, Amazon S3, AWS Security Hub, Azure Service Bus, Azure Logic Apps, Sumo Logic, and CrowdStrike Falcon Next-Gen SIEM**.

## Where connector setup meets routing

Configure destinations at the app route above, then use the relevant automation or notification workflow to select how they are used. The Automations directory helps distinguish stored connectors from schedules, runs, and event-routing rules; it is not the connector editor.

{% include screenshot.html file="admin-automations-workflow-directory.png" title="Connector consumers — automation and notification entry points" caption="The directory shows a seeded connector count separately from zero schedules and runs, with a Notifications card describing event routing. A stored connector is not proof of an enabled rule or successful delivery. This local synthetic example did not configure, test, or send to an external destination." %}

## Setup guides

- [Teams, Slack, and email]({{ site.baseurl }}/connectors/messaging/)
- [Jira, ServiceNow, and PagerDuty]({{ site.baseurl }}/connectors/ticketing-on-call/)
- [SIEM and security destinations]({{ site.baseurl }}/connectors/siem-security/)
- [Grafana]({{ site.baseurl }}/connectors/grafana/)
- [Azure Logic Apps and generic webhook]({{ site.baseurl }}/connectors/webhooks-logic-apps/)
- [Queues and storage]({{ site.baseurl }}/connectors/queues-storage/)

## Procedures

| Task | Recipe |
| --- | --- |
| Create, test, enable, rotate, disable, or delete a connector | [Manage connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) |
| Configure Teams, Slack, Outlook, or email | [Configure messaging connectors]({{ site.baseurl }}/how-to/automations-connectors/connectors-messaging/) |
| Configure Jira, ServiceNow, or PagerDuty | [Configure ticketing connectors]({{ site.baseurl }}/how-to/automations-connectors/connectors-ticketing/) |
| Configure Grafana, Splunk, Sumo Logic, CrowdStrike, or AWS Security Hub | [Configure observability connectors]({{ site.baseurl }}/how-to/automations-connectors/connectors-observability/) |
| Configure Azure Logic Apps | [Configure Logic Apps connector]({{ site.baseurl }}/how-to/automations-connectors/connectors-orchestration/) |
| Configure Service Bus, Amazon SQS, or S3 | [Configure queue and storage connectors]({{ site.baseurl }}/how-to/automations-connectors/connectors-queues-storage/) |
| Configure a generic webhook | [Configure custom webhook connector]({{ site.baseurl }}/how-to/automations-connectors/connectors-custom/) |
| Route events to a connector | [Route and review notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/) |

## Safe setup

1. Create a least-privilege account, token, policy, or endpoint at the destination.
2. Create the connector disabled and enter no real IDs in documentation.
3. Save, run the side-effect-free **Test** probe, and inspect status detail.
4. Use **Send test** only for types where the UI offers it; it creates a real destination event.
5. Verify at the destination, then enable and select it in a notification/task rule.

Tests for ticketing/storage/queue connectors avoid writes where possible. A presence-only test does not prove delivery. Connector tools remain subject to tool classification, approvals, tenant scope, and audit.
