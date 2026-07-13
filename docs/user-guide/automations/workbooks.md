---
layout: default
title: Workbooks
parent: Automations
grand_parent: User guide
nav_order: 2
description: Build and run reusable parameterized operations with structured and AI-assisted outputs.
permalink: /user-guide/automations/workbooks/
---

# Workbooks

**Permissions:** `workbooks.read`, `workbooks.write`

## Purpose

**App route:** `/automations/workbooks`
A workbook is a saved parameterized operation using an implemented runtime such as Azure CLI, KQL/Resource Graph, or PowerShell. Results can be summarized, severity-classified, schema-extracted, compared with a previous run, shown as dashboard tiles, or consumed by a playbook.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

1. Create from a reviewed template, import a JSON bundle, or use the AI designer.
2. Select the runtime and write the bounded operation.
3. Define parameters with safe defaults; never place secrets in definitions.
4. Configure structured output and optional AI summary/classification.
5. Preview interpolation, then run against a non-production or read-only scope first.
6. Inspect raw output, structured fields, severity, errors, duration, and run history.
7. Export a portable bundle or reference it from a task/playbook.

AI generation and enhancement create drafts. Validate every command, query, parameter, and scope before saving or running.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

Connection read-only state blocks destructive command execution. Write operations require explicit confirmation/approval according to policy. PowerShell and CLI operations still carry the authority of the selected connection and runtime host. Use narrow allowlists and timeouts.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| **Interpolation error | ** verify parameter names and required values. |
| **Runtime unavailable | ** confirm executable/tool configuration and selected connection. |
| **Structured output empty | ** inspect raw output and extraction schema. |
| **Diff missing | ** a compatible previous successful run is required. |
| **Import rejected | ** validate bundle format and referenced runtime/parameters. |

## Related pages

- [Playbooks]({{ site.baseurl }}/user-guide/automations/playbooks/)
- [Scheduled Tasks]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
