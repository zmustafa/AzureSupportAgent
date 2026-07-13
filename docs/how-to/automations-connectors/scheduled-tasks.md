---
layout: default
title: Schedule and operate tasks
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 61
description: Create, validate, run, pause, archive, restore, and troubleshoot scheduled tasks.
permalink: /how-to/automations-connectors/scheduled-tasks/
---

# Schedule and operate tasks

## Prerequisites

- `tasks.read` and `tasks.write`; `tasks.run` to run immediately.
- An existing agent, assessment, workbook, or playbook target.
- An enabled connector only if the result must be delivered externally.
- `tasks.read` and `tasks.write`.
- A clear retention decision before permanent deletion.

## Route

- Open `/automations/tasks`.

## How to create and validate a scheduled task

1. Select **New schedule**, name the task, and choose its target type and target.
2. Supply the target options shown by the form.
3. Choose daily, weekly, or custom recurrence; select the intended IANA time zone and optional start/end dates.
4. For custom recurrence, use the recurrence builder and review the live schedule label and next five occurrences.
5. Choose **Review** or **Autonomous** run mode and the thread grouping behavior appropriate for the target.
6. Select notification connectors only when the run summary should leave the app.
7. Save the schedule. Use **Save & run now** only after reviewing the target and destination effects.

**Expected result:** The task appears with its status, human-readable schedule, next run, and target.

**Verification:** Open **Run history** after a due or manual run. Confirm status, timestamps, target link, and any per-connector delivery result. Recheck previewed times around daylight-saving transitions.

## How to pause, archive, restore, or permanently delete a task

1. Use the task's status control to pause or resume it without changing its definition.
2. Select **Delete** and confirm to archive a schedule; archived schedules do not run.
3. In **Archived schedules**, inspect **History** before taking further action.
4. Select **Restore** to return the schedule as paused, then review and enable it if still valid.
5. Select **Delete permanently** only when both the schedule and its run history may be removed.

**Expected result:** Paused and archived tasks stop future execution; restored tasks return paused; permanent deletion removes the schedule and history.

**Verification:** Confirm the status or archived-list placement and ensure no new run is scheduled.

## Safety and rollback

Pause is immediately reversible. Archive is reversible through **Restore**. Permanent deletion cannot be rolled back; export or retain required evidence before confirming it.

Create risky schedules paused, validate manually, then enable. Pause the task to stop future runs. Archiving also stops runs and preserves history; restore returns an archived task in a paused state.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Task still runs after pause | refresh and confirm its displayed status; inspect whether a run was already in progress. |
| Restored task does not run | restoration intentionally pauses it; review then enable it. |
| History is absent | confirm the correct task and whether it was permanently deleted. |
| Invalid or missing next run | correct the recurrence, time zone, and start/end window. |
| Manual run unavailable | request `tasks.run` separately from read/write permissions. |
| Run remains pending | check other concurrent work and scheduler health. |
| Delivery fails | verify the connector is enabled and use its provider-specific verification guide. |
| [Scheduled Tasks overview]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/) | Review connector configuration and retry. |
| [Manage connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
- [Playbooks]({{ site.baseurl }}/how-to/automations-connectors/playbooks/)
