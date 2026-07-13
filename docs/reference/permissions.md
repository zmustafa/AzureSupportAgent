---
layout: default
title: Permissions
parent: Reference
nav_order: 1
description: Map product capability strings to major user, automation, investigation, integration, and administration surfaces.
permalink: /reference/permissions/
---

# Permissions

Permissions are explicit strings checked by API routes. Custom roles select them in Settings → Access Control → Roles. The live catalog is authoritative.

| Area | Capabilities |
| --- | --- |
| Agent | `chat.use` |
| Automation | `agents.read`, `agents.write`, `tasks.read`, `tasks.write`, `tasks.run`, `workbooks.read`, `workbooks.write`, `playbooks.read`, `playbooks.write`, `insights.read`, `insights.write`, `insights.run`, `notifications.read`, `notifications.manage` |
| Workloads/design | `workloads.read`, `workloads.write`, `architectures.read`, `architectures.write`, `missions.read`, `missions.run`, `ownership.read`, `ownership.write` |
| Estate/investigation | `inventory.read`, `graph.read`, `changeexplorer.read`, `reservations.read`, `perfprofile.read`, `radar.read`, `quota.read`, `quota.run`, `tagintel.read`, `tagintel.write`, `evidence.read`, `evidence.write`, `cases.read`, `cases.write` |
| Governance | `assessments.read`, `assessments.run`, `policy.read`, `policy.write`, `rbac.read`, `identity.read` |
| Observability | `monitor.view`, `coverage.read`, `coverage.manage`, `teleintel.read`, `alert_analysis.read`, `alert_analysis.manage`, and action-specific `alerts_manager.*` capabilities |
| Diagnostics | `sandbox.exec`, `netdiag.run` |
| Integrations | `connections.read`, `connections.manage`, `connectors.manage` |
| Administration | `settings.read`, `settings.write`, `users.manage`, `audit.read`, `backup.manage`, `demo.manage` |

Alerts Manager deliberately separates read, alert-state write, action-group write, rule write, advanced/bulk/AMBA changes, query preview, notification test, delete/rollback, and approval.

Built-in role intent is documented in [Access control]({{ site.baseurl }}/security/access-control/). Product permission does not replace Azure RBAC, Microsoft Graph consent, connection read-only policy, or write approval.
