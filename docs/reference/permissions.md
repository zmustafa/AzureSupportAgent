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
| Governance | `assessments.read`, `assessments.run`, `policy.read`, `policy.write`, `iam.read`, `identity.read` |
| Observability | `monitor.view`, `coverage.read`, `coverage.manage`, `teleintel.read`, `alert_analysis.read`, `alert_analysis.manage`, action-specific `alerts_manager.*` capabilities, and `backup_manager.read`, `backup_manager.protect_write`, `backup_manager.policy_write`, `backup_manager.vault_write`, `backup_manager.ondemand`, `backup_manager.drill_write`, `backup_manager.reference_write`, `backup_manager.approve` |
| Diagnostics | `sandbox.exec`, `netdiag.run` |
| Integrations | `connections.read`, `connections.manage`, `connectors.manage` |
| Administration | `settings.read`, `settings.write`, `users.manage`, `audit.read`, `backup.manage`, `demo.manage` |

Alerts Manager deliberately separates read, alert-state write, action-group write, rule write, advanced/bulk/AMBA changes, query preview, notification test, delete/rollback, and approval.

Backup Manager separates by target as well as by verb: protecting an item, editing a policy, hardening a vault, running an on-demand backup, recording a drill, and curating the reference are distinct capabilities. `backup_manager.read` covers every read, including the Fleet grid and starting an analysis — an analysis is a read-only sweep. `backup_manager.approve` decides and applies managed changes, and is also required to permanently purge stored analyses or analysis history in Cleanup.

Built-in role intent is documented in [Access control]({{ site.baseurl }}/security/access-control/). Product permission does not replace Azure RBAC, Microsoft Graph consent, connection read-only policy, or write approval.
