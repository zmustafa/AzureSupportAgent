---
layout: default
title: Automations and connectors
parent: How-to guides
nav_order: 6
description: Task-focused guides for schedules, workbooks, playbooks, notifications, and all supported connectors.
permalink: /how-to/automations-connectors/
has_children: true
feature_ids: [SHELL_NAV:automations]
---

# Automations and connectors

Use these guides to build reusable operations, schedule them, route their results, and connect all 18 implemented external destinations.

## Automation workflows

- [Schedule and operate tasks]({{ site.baseurl }}/how-to/automations-connectors/scheduled-tasks/)
- [Build and run workbooks]({{ site.baseurl }}/how-to/automations-connectors/workbooks/)
- [Build and run playbooks]({{ site.baseurl }}/how-to/automations-connectors/playbooks/)
- [Route and review notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
- [Create and manage Sub Agents]({{ site.baseurl }}/how-to/automations-connectors/sub-agents/)

## Connector workflows

Start with [Manage connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/). Provider recipes follow the same categories as the connector gallery:

- [Messaging and ChatOps]({{ site.baseurl }}/how-to/automations-connectors/connectors-messaging/) — Microsoft Teams, Slack, Microsoft Outlook, Email (SMTP)
- [Ticketing and ITSM]({{ site.baseurl }}/how-to/automations-connectors/connectors-ticketing/) — Jira, ServiceNow, PagerDuty
- [Observability and SIEM]({{ site.baseurl }}/how-to/automations-connectors/connectors-observability/) — Splunk, Grafana, AWS Security Hub, Cortex XSOAR, Sumo Logic, CrowdStrike Next-Gen SIEM
- [Queues and storage]({{ site.baseurl }}/how-to/automations-connectors/connectors-queues-storage/) — Azure Service Bus Queue, Amazon SQS, Amazon S3
- [Automation and orchestration]({{ site.baseurl }}/how-to/automations-connectors/connectors-orchestration/) — Azure Logic Apps
- [Custom webhooks]({{ site.baseurl }}/how-to/automations-connectors/connectors-custom/) — Webhook

The implemented type identifiers are `teams`, `slack`, `outlook`, `email`, `jira`, `servicenow`, `pagerduty`, `splunk`, `grafana`, `securityhub`, `xsoar`, `sumologic`, `crowdstrike_ngsiem`, `servicebus`, `sqs`, `s3`, `logicapp`, and `webhook`.

## Test versus Send test

**Test** is configuration-only or a lightweight authentication/read probe. It does not intentionally create tickets, messages, incidents, findings, queue messages, or storage objects. A successful configuration-only test proves only that required values are present.

**Send test** performs a real delivery and can have downstream effects. The UI and API support it only for this allowlist: Teams, Slack, Email (SMTP), Outlook, Webhook, PagerDuty, Splunk, Grafana, Logic Apps, Sumo Logic, and CrowdStrike Next-Gen SIEM. The button is not available for Jira, ServiceNow, XSOAR, Service Bus, SQS, S3, or Security Hub; use each guide's safe verification procedure instead.

## Permissions

Connector management requires `connectors.manage`. Feature pages list their own permissions. Use least-privilege provider identities and non-production destinations while validating a new integration.
