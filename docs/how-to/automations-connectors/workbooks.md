---
layout: default
title: Build and run workbooks
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 62
description: Create, test, run, import, export, and troubleshoot reusable Azure operations.
permalink: /how-to/automations-connectors/workbooks/
feature_ids: [AUTOMATIONS_NAV:workbooks]
---

# Build and run workbooks

## Prerequisites

- `workbooks.read` to list/export/history; `workbooks.write` to author, import, use AI design, test, and run. The current UI disables the entire panel without write access, including History/Export.
- An Azure connection appropriate for the intended scope.
- A reviewed Resource Graph query, Azure CLI command, or PowerShell operation.
- For CLI/PowerShell, administrator-enabled command execution, an installed allowlisted binary, and reviewed identity behavior. Resource Graph is a read-only query path, not Log Analytics.
- An active AI provider for drafting or AI output processing; raw execution can be reviewed with AI output disabled.
- A reviewed JSON workbook bundle from a trusted source.

## Route

- Open `/automations/workbooks`.

**Screenshot note:** These are browser-only catalog and history examples, not executed workbook operations. No Azure query, host command or AI request ran. The parameter dialog was opened without selecting **Run workbook**; its recent results were supplied separately as synthetic history.

{% include screenshot.html file="fpa-workbooks-catalog.png" title="Choose a workbook and inspect its runtime" caption="Locate the intended workbook, check its runtime, and use Edit to review its definition before execution. The example recovery workbook collects material for manual review; the catalog does not show a restore, remediation or provider compatibility test." %}

## How to create and test a workbook

1. Select **New workbook**, edit a reviewed starter, or use **Generate with AI**. Submit each interview step with **Continue**; **Generate now** skips remaining questions and does not include unsubmitted current-step answers. Treat the result as an untrusted draft.
2. Name the workbook and choose **Resource Graph (KQL)**, **Azure CLI**, or **PowerShell**.
3. {% raw %}Enter the body and define each `{{parameter}}` with a key, label, and safe default. Supply every required value yourself: required markers do not enforce validation, missing placeholders become empty strings, and values are not automatically escaped.{% endraw %}
4. Select a default Azure connection and optional tags.
5. Configure AI output modes: summary, severity, extraction schema, or diff.
6. Optionally enable an alert event with a minimum severity or a dashboard tile.
7. Enter test parameter values and select **Test run** only after reviewing execution effects. It executes the draft with confirmation enabled, does not persist a workbook run, and does not emit the workbook's alert event.
8. Inspect raw output, structured output, narrative, severity, duration, and errors; then save.

{% include screenshot.html file="fpa-workbook-editor.png" title="Review the query, default scope, and output settings" caption="The unsaved editor shows a read-only Resource Graph query, synthetic connection and parameter default, AI output modes, and alert/tile options. Save and Test run were not selected; configured AI processing is not evidence that extraction or execution succeeded. Inspect every parameter key and label in your own editor before running." %}

**Expected result:** A reusable workbook is saved, and its draft test returns an understandable result without adding to run history.

**Verification:** Select **Run**, enter parameter values, then **Run workbook**. Open **History**, expand the new record, and compare **Command**, raw output, structured result, severity, and error with the test. The saved Run action does not pass write confirmation, unlike Test run.

{% include screenshot.html file="fpa-workbook-run-parameters.png" title="Review saved-run parameters without starting execution" caption="The Run dialog shows the resource-group input and recent successes and failure for context. Run workbook was not clicked during capture; a blocked saved write requires an approved execution process, not switching to Test run to bypass confirmation." %}

## How to verify a structured result, diff, tile, or alert

1. In **Edit**, enable **AI'fy the output** and choose **Extract to schema**. Describe the intended fields, then validate the returned JSON rather than assuming the AI follows a strict schema.
2. Select **Diff vs last run** only when comparing equivalent scopes and parameter sets. Execute two reviewed saved runs and inspect changes in History; the comparison uses the latest successful run's top-level structured keys.
3. For a **Number** dashboard tile, set its metric key to an actual top-level JSON key. Check the latest result and tile timestamp; a tile does not refresh Azure or schedule the workbook by itself.
4. If alerts are required, enable **Emit alert event**, set minimum severity, and verify a controlled saved result that meets that threshold against the notification rules.

**Expected result:** Structured fields feed a shallow diff/tile, and qualifying saved results emit workbook events.

**Verification:** Distinguish execution status from AI severity. No earlier structured result means keys appear as additions; unavailable AI/extraction can leave the tile empty. The AI processes bounded output, so inspect raw evidence before relying on a summary.

{% include screenshot.html file="fpa-workbook-structured-result.png" title="Inspect structured fields before using a diff or tile" caption="The expanded example shows count, total, resource group and names alongside a prior-run change summary. It is synthetic saved evidence, not a new execution or AI extraction; verify the raw output and Command disclosures separately before trusting derived fields." %}

## How to import, export, and reuse a workbook

1. Select **Export** on a workbook and review the downloaded definition bundle. It contains no run history, but body/defaults, connection/tenant fields, and alert/tile settings can retain environment information. Sanitize before sharing.
2. In the destination environment, select **Import** and choose the reviewed bundle.
3. Read the import result: a single-workbook import creates a new definition, suffixing a colliding name rather than updating it. Open **Edit** and verify runtime, body, parameters, connection, alerts, and tile settings before executing anything.
4. Use **Test run** with a non-production or read-only scope.
5. Save any environment-specific corrections, then run once and inspect **History**.
6. Reference the validated workbook from a playbook or scheduled task.

**Expected result:** The workbook is portable while environment-specific scope and connection choices remain explicitly reviewed.

**Verification:** Find the exact newly named workbook, verify the saved connection, and inspect its first persisted command/output. Repeating the import creates another definition. Import does not repair references to an old workbook ID.

## How to retire a workbook without losing track of prior effects

1. Pause schedules that reference the workbook and review every playbook that uses it.
2. Open **History** and retain any required evidence through your approved process; the workbook Export action exports the definition, not these runs.
3. Export the reviewed definition if needed, then select **Delete** only when removal is intended. The current Delete action is immediate and has no confirmation or trash step.
4. Check referencing tasks/playbooks and any external changes from earlier executions separately.

**Expected result:** The workbook definition is removed; historical database rows and external effects are not undone.

**Verification:** Confirm the card is absent and referencing schedules are paused. There is no restore button; importing a bundle creates a new ID, not the original history association.

## Safety and rollback

Definition edits/imports write application state. Test run and saved runs execute the body and may affect Azure or the host. AI actions call the configured provider. Use read-only operations first; never store secrets in body/defaults.

The command runner uses an allowlist, syntax restrictions, and recognized mutating tokens—not the workbook's `kind` label or a PowerShell sandbox. Recognized writes require a writable connection and confirmation. Saved Run has no approval continuation; do not use Test run as a workaround for a rejected saved write.

History is persisted after execution. An interrupted request or missing history does not prove the external operation failed. Inspect destination state before another full run; there is no cancellation, checkpoint/resume, retry-failed, or automatic rollback here.

## Troubleshooting

| Symptom | Cause and resolution |
| --- | --- |
| Import rejected | Check the version-1 `azsupagent.bundle` envelope, kind `workbook`, and named workbook object. Acceptance does not validate operation safety. |
| Wrong environment identifiers | Definition bundles retain body/defaults and connection references. Correct them before running and verify that the connection did not fall back to the deployment default. |
| Import succeeds but the card is missing | Import retains the bundle's tenant field, while the list filters by current tenant/global scope. Ask an administrator to review placement; recreate a reviewed definition through New workbook in the intended tenant if needed. |
| Referenced playbook fails | Import creates a new workbook ID; existing steps can still point to the old one. Select the intended workbook on each affected step and verify its parameters. |
| Missing value | {% raw %}Align keys with `{{parameter}}` placeholders and explicitly supply values; missing substitution becomes empty text.{% endraw %} |
| Empty extraction | AI may be unavailable, Extract to schema may be off, or the response may not contain the requested JSON. Check raw output, enable extraction, and revise the schema hint before relying on a tile/map. |
| Runtime failure | Read the captured error for disabled execution, disallowed/missing binary, timeout, or authorization failure. Review the specific policy/runtime/connection issue before executing again. |
| Write rejected | Saved Run omits confirmation; a recognized mutating command fails rather than opening an approval dialog. Review the approved execution process instead of switching to Test run. |
| Diff misleading | Check scope/parameters/schema against the latest successful run. There is no compatibility check before comparison. |
| No completed history after a connection loss | Execution may already have happened. Check destination state before rerunning; Test run intentionally records no workbook history. |

## Related docs

- [Scheduled tasks]({{ site.baseurl }}/how-to/automations-connectors/scheduled-tasks/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Workbooks overview]({{ site.baseurl }}/user-guide/automations/workbooks/)
- [Build and run playbooks]({{ site.baseurl }}/how-to/automations-connectors/playbooks/)
