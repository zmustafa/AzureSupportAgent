---
layout: default
title: Change General settings safely
parent: Administration tasks
grand_parent: How-to guides
nav_order: 55
description: Adjust runtime, retention, safety, tool, command, coverage, and feature settings with bounded verification.
permalink: /how-to/administration/general-settings/
---

# Change General settings safely

## Prerequisites

- Product permission `settings.write`.
- An approved reason, current value, expected value, and representative verification workflow.
- A current backup for broad or high-impact changes.

## Route

- Open `/admin/audit`.
- Open `/admin/settings`.

## How to change an application setting

1. Find the setting by its visible label. Configure only controls present in the current build.
2. Record the current value and the affected workflows.
3. Change the smallest related set. High-impact controls include MCP read-only mode, Entra MCP enablement, automatic writes, built-in network egress, command execution, Sandbox tooling, retention, approved telemetry workspaces, concurrency, timeouts, and scan limits.
4. Save and review the values returned by the page; numeric bounds, lists, schedules, time zones, durations, thresholds, and colors are normalized or validated by the backend.
5. Reopen `/admin/settings` and confirm the effective value.
6. Run one bounded workflow affected by the change.
7. Review `/admin/audit` for `settings.update`.

**Expected result:** The validated value persists and the representative workflow changes only as intended.

**Verification:** Confirm the saved value after reload, inspect the workflow result for truncation/timeouts or newly exposed actions, and check the audit timestamp and actor.

## Safety and rollback

Validate changes in a non-production scope first, and preserve a known-good configuration for rollback.

Keep MCP read-only and automatic-write protections aligned with organizational approvals. Command and Sandbox toggles expose execution surfaces; network allow/deny lists affect egress. To roll back, restore the recorded value, save, rerun the same verification, or restore the prior configuration backup when many settings changed.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Unexpected result | Re-check route, permissions, and latest refresh state before retrying. |
| Value changes after save | Use the returned value; backend validation may clamp or normalize the input. |
| A tool disappears | Check MCP read-only, Entra enablement, built-in disabled tools, and egress controls. |
| A scan is incomplete | Review concurrency, per-check timeout, run budget, row cap, scope, and connection capability. |
| Schedule does not run when expected | Check the visible recurrence, `HH:MM` value, IANA time zone, recipients, and enabled state. |

## Related docs

- [How-to guides]({{ site.baseurl }}/how-to/)
- [General settings reference]({{ site.baseurl }}/admin/general-settings/)
- [Azure and Entra MCP tools]({{ site.baseurl }}/how-to/administration/mcp-tools/)
- [Backup and Restore]({{ site.baseurl }}/how-to/administration/backup-demo/)
