---
layout: default
title: Build and run playbooks
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 63
description: Chain workbooks with severity gates, validate runs, and move playbooks safely.
permalink: /how-to/automations-connectors/playbooks/
---

# Build and run playbooks

## Prerequisites

- `playbooks.read` and `playbooks.write`.
- Saved, individually tested workbooks for every step.
- `playbooks.read` to export; `playbooks.write` to import and run.
- `tasks.write` to schedule and `tasks.run` to validate immediately.

## Route

- Open `/automations/playbooks`.
- Open `/automations/tasks`.

## How to build and validate a playbook

1. Select **New playbook**, or use **Generate with AI** and review the generated draft and proposed workbooks.
2. Name the playbook and add steps in dependency order.
3. Select a workbook for each step.
4. Set **Always run** or a running-severity threshold of warning, error, or critical.
5. Where supported by the draft/editor, map static parameters and structured output from earlier steps; ensure the producer emits each referenced key.
6. Optionally emit a completion notification event and choose its minimum severity.
7. Save, select **Run**, and confirm execution.
8. Open **History** and expand the run to inspect each succeeded, failed, or skipped step and its reason.

**Expected result:** Enabled steps execute in order; severity gates skip steps below their threshold; the run records the worst severity and per-step outcomes.

**Verification:** Run at least one healthy and one warning/error scenario. Confirm gates, mappings, failure stopping behavior, and the final notification event if enabled.

## How to import, export, and schedule a playbook

1. Select **Export** to download the playbook bundle, including its referenced workbooks.
2. In the destination environment, select **Import** and choose the reviewed bundle.
3. Inspect the imported playbook and every imported workbook for commands, identifiers, parameters, and connection scope.
4. Run each workbook independently, then run the playbook and inspect per-step history.
5. Go to `/automations/tasks`, create a playbook-target schedule, and save it paused.
6. Use **Run now**, inspect task and playbook histories, then enable the schedule.

**Expected result:** The playbook and required workbooks are available in the destination and execute correctly before recurrence is enabled.

**Verification:** Confirm references resolve, no required step is missing, and the scheduled run links to the expected playbook result.

## Safety and rollback

Imported portability does not prove provider compatibility. Pause or archive the schedule first, then remove an invalid playbook only after checking references. External writes require provider-specific rollback.

A gate is workflow logic, not authorization. Every step retains its workbook's provider permissions and side effects. Keep detection and remediation separate. Edit or delete the playbook to stop future manual use, and pause any scheduled task that references it; reverse provider-side changes separately.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Missing workbook | re-import the complete playbook export and review import feedback. |
| Duplicate-looking workbook | compare definitions and references before deleting either copy. |
| Scheduled run differs from manual run | compare connection, parameter, run mode, and actor context. |
| Unexpected skip | inspect the accumulated severity before that step and its threshold. |
| Empty mapping | enable structured extraction in the producing workbook and match its key exactly. |
| Run stops | inspect the first failed step; later steps are not a recovery mechanism unless explicitly designed. |
| Wrong scope | verify each workbook's connection and parameters, not only the playbook. |
| [Playbooks overview]({{ site.baseurl }}/user-guide/automations/playbooks/) | Review connector configuration and retry. |
| [Build and run workbooks]({{ site.baseurl }}/how-to/automations-connectors/workbooks/) | Review connector configuration and retry. |

## Related docs

- [Schedule and operate tasks]({{ site.baseurl }}/how-to/automations-connectors/scheduled-tasks/)
- [Notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
