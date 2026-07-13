---
layout: default
title: Build and run workbooks
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 62
description: Create, test, run, import, export, and troubleshoot reusable Azure operations.
permalink: /how-to/automations-connectors/workbooks/
---

# Build and run workbooks

## Prerequisites

- `workbooks.read` and `workbooks.write`.
- An Azure connection appropriate for the intended scope.
- A reviewed Resource Graph query, Azure CLI command, or PowerShell operation.
- `workbooks.read` to export and `workbooks.write` to import.
- A reviewed JSON workbook bundle from a trusted source.

## Route

- Open `/automations/workbooks`.

## How to create and test a workbook

1. Select **New workbook**, or use **Generate with AI** and treat its output as an untrusted draft.
2. Name the workbook and choose **Resource Graph (KQL)**, **Azure CLI**, or **PowerShell**.
3. Enter the body and define each `{{parameter}}` shown in it, using safe defaults.
4. Select a default Azure connection and optional tags.
5. Configure AI output modes: summary, severity, extraction schema, or diff.
6. Optionally enable an alert event with a minimum severity or a dashboard tile.
7. Enter test parameter values and select **Test run**. This executes the draft but does not persist a workbook run.
8. Inspect raw output, structured output, narrative, severity, duration, and errors; then save.

**Expected result:** A reusable workbook is saved, and its draft test returns an understandable result without adding to run history.

**Verification:** Run the saved workbook once from its **Run** action, then open **History** and compare the persisted result with the test result.

## How to import, export, and reuse a workbook

1. Select **Export** on a workbook and store the downloaded JSON according to your change process.
2. In the destination environment, select **Import** and choose the reviewed bundle.
3. Open the imported workbook and verify runtime, body, parameters, connection, alerts, and tile settings.
4. Use **Test run** with a non-production or read-only scope.
5. Save any environment-specific corrections, then run once and inspect **History**.
6. Reference the validated workbook from a playbook or scheduled task.

**Expected result:** The workbook is portable while environment-specific scope and connection choices remain explicitly reviewed.

**Verification:** Confirm the imported workbook appears once, its test succeeds, and its first persisted run uses the intended connection and parameters.

## Safety and rollback

An exported bundle can contain operational commands and defaults; handle it as configuration. Delete the imported workbook if review fails. Deleting it does not undo external changes from prior runs.

A test run executes the body even though it does not persist history. Use read-only scope first. Review generated code and parameters; never place secrets in the body or defaults. Delete or edit the workbook to roll back its definition, but separately reverse any provider-side write it executed.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Import rejected | use an unmodified workbook export and validate the JSON file. |
| Wrong environment identifiers | edit connection and parameter defaults before running. |
| Referenced playbook fails | confirm it points to the imported workbook and required parameters still match. |
| Missing value | align parameter keys exactly with `{{parameter}}` placeholders. |
| Empty extraction | inspect raw output and revise the extraction schema. |
| Runtime failure | verify the selected connection, executable availability, and provider permission. |
| Write rejected | review connection read-only policy and required confirmation. |
| [Workbooks overview]({{ site.baseurl }}/user-guide/automations/workbooks/) | Review connector configuration and retry. |
| [Build and run playbooks]({{ site.baseurl }}/how-to/automations-connectors/playbooks/) | Review connector configuration and retry. |

## Related docs

- [Scheduled tasks]({{ site.baseurl }}/how-to/automations-connectors/scheduled-tasks/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
