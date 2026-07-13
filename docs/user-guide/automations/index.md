---
layout: default
title: Automations
parent: User guide
nav_order: 9
description: Schedule recurring targets, run reusable workbooks and playbooks, and route notifications.
permalink: /user-guide/automations/
has_children: true
feature_ids: [SHELL_NAV:automations]
---

# Automations

Automations turns reviewed operations into repeatable runs. It includes scheduled tasks, workbooks, playbooks, and notification rules. Permissions are separate for viewing, editing, and running where the product supports that distinction.

| Guide | Purpose |
| --- | --- |
| [Scheduled Tasks]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/) | Run agents, assessments, workbooks, or playbooks on a recurrence. |
| [Workbooks]({{ site.baseurl }}/user-guide/automations/workbooks/) | Save parameterized Azure CLI, Resource Graph/KQL, or PowerShell operations. |
| [Playbooks]({{ site.baseurl }}/user-guide/automations/playbooks/) | Chain workbooks with conditions and output mappings. |
| [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/) | Manage the in-app center and event-routing rules. |
| [Sub Agents]({{ site.baseurl }}/user-guide/automations/sub-agents/) | Configure reusable personas, models, and least-privilege tool sets. |

## Safe automation pattern

Start manually, constrain scope and parameters, inspect output, then schedule. Keep Azure connections read-only unless an approved process requires writes. A schedule does not remove tool write classification, connection policy, approval requirements, tenant isolation, or audit logging.
