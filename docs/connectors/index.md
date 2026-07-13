---
layout: default
title: Connectors
nav_order: 21
description: Configure implemented messaging, ticketing, monitoring, SIEM, security, queue, storage, and automation destinations.
permalink: /connectors/
has_children: true
---

# Connectors

**App route:** `/admin/connectors`
**Permission:** `connectors.manage`

Connector definitions are encrypted at rest. Secret fields are masked on read; entering a blank secret during edit keeps the existing value. Each connector has type, mode, name, enabled/disabled state, health status, and type-specific fields.

The source registers these exact types: **Teams, Outlook, Email, Jira, ServiceNow, Grafana, Slack, Webhook, PagerDuty, Splunk, Cortex XSOAR, Amazon SQS, Amazon S3, AWS Security Hub, Azure Service Bus, Azure Logic Apps, Sumo Logic, and CrowdStrike Falcon Next-Gen SIEM**.

## Setup guides

- [Teams, Slack, and email]({{ site.baseurl }}/connectors/messaging/)
- [Jira, ServiceNow, and PagerDuty]({{ site.baseurl }}/connectors/ticketing-on-call/)
- [SIEM and security destinations]({{ site.baseurl }}/connectors/siem-security/)
- [Grafana]({{ site.baseurl }}/connectors/grafana/)
- [Azure Logic Apps and generic webhook]({{ site.baseurl }}/connectors/webhooks-logic-apps/)
- [Queues and storage]({{ site.baseurl }}/connectors/queues-storage/)

## Safe setup

1. Create a least-privilege account, token, policy, or endpoint at the destination.
2. Create the connector disabled and enter no real IDs in documentation.
3. Save, run the side-effect-free **Test** probe, and inspect status detail.
4. Use **Send test** only for types where the UI offers it; it creates a real destination event.
5. Verify at the destination, then enable and select it in a notification/task rule.

Tests for ticketing/storage/queue connectors avoid writes where possible. A presence-only test does not prove delivery. Connector tools remain subject to tool classification, approvals, tenant scope, and audit.
