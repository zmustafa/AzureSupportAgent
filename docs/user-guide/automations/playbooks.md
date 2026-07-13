---
layout: default
title: Playbooks
parent: Automations
grand_parent: User guide
nav_order: 3
description: Chain workbooks into conditional, parameter-mapped, observable multi-step flows.
permalink: /user-guide/automations/playbooks/
---

# Playbooks

**Permissions:** `playbooks.read`, `playbooks.write`

## Purpose

**App route:** `/automations/playbooks`
A playbook chains existing workbooks. Steps can use severity-based `run_if` gates and map structured output from an earlier step into parameters for a later step. References follow the step/output pattern shown by the editor.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Build and run

1. Create manually, import a bundle, or generate a draft through the AI interview.
2. Add steps in dependency order and select a workbook for each.
3. Map playbook inputs and previous structured outputs into workbook parameters.
4. Add explicit conditions for steps that should skip on healthy/informational results.
5. Validate missing references and cycles, then run manually.
6. Inspect per-step status, inputs, raw/structured result, skipped reason, and final result.
7. Schedule only after repeated manual success.

Export bundles inline referenced workbooks for portability. Review imported workbook commands because portability does not guarantee that target environments, permissions, or identifiers match.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

Every step retains the workbook's runtime and write risk. A condition is orchestration logic, not an authorization boundary. Keep remediation and verification as separate steps, fail closed when required output is absent, and do not map secret-bearing output into logs or notifications.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Step skipped unexpectedly | Inspect `run_if` and the prior step's normalized severity/status. |
| Mapping is empty | Confirm the producer emits the named structured key. |
| Playbook stops | Inspect the first failed step; downstream required dependencies do not run. |
| Imported playbook is invalid | Import referenced workbooks together and resolve duplicate/missing IDs. |

## Related pages

### Related docs

- [Workbooks]({{ site.baseurl }}/user-guide/automations/workbooks/)
- [Scheduled Tasks]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/)
