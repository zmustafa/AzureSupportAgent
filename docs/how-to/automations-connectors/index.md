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

{% include screenshot.html file="admin-automations-workflow-directory.png" title="Automations directory for reusable operations and delivery" caption="Choose schedules, workbooks, playbooks, or notifications from the capability directory. Seeded demo connectors and zero schedule/run counts are local fresh-install state, not execution or delivery evidence; no workflow ran and no provider, OAuth, or Azure connectivity was verified." %}

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

Task authoring uses `tasks.write`, but immediate execution separately needs `tasks.run` and history needs `tasks.read`. Workbook and playbook execution use their respective write permissions. Agent exports use `agents.read` at the API, while the current UI's read-only panel disables export controls without `agents.write`. Notification feed/read-state access (`notifications.read`) is separate from rule management (`notifications.manage`); read state is shared within the tenant, not personal.

## How to validate the whole automation chain

1. Follow the workbook, playbook, or Sub Agent guide above to inspect scope, parameters, runtime, and write policy before any execution.
2. Run a reviewed read-only operation and inspect its actual output. Workbook **Test run** is real execution without workbook history, not a harmless preview.
3. Create a paused scheduled task and use **Run now** only after checking for existing runs. Inspect both task history and the target's evidence.
4. Configure a disabled notification rule, verify the connector separately, then enable the rule for one controlled producer event.
5. Confirm the event in-app and at the intended destination before enabling recurrence. Avoid selecting both direct task delivery and rule routing to the same destination unless duplicate messages are intentional.

**Expected result:** Each link—definition, execution, recurrence, rule match, and external delivery—is verified independently.

**Verification:** A saved definition, Task started acknowledgement, success badge, and connector Test each prove different things. Check actual outputs and destinations; none alone proves the whole chain completed.

## Recovery and evidence

Pause schedules before changing or deleting their referenced definitions. Task archive is reversible and retains task history; permanent deletion is not. Workbook/playbook deletion has no archive/restore, and replaying a failed playbook starts the full sequence rather than only failed steps. Disabling a Sub Agent does not stop its scheduled or existing-chat references. Notification delivery has no resend/retry screen and cannot retract external artifacts. The linked feature guides describe these limits and the evidence to inspect before another run.
