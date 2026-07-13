---
layout: default
title: Sub Agents
parent: Automations
grand_parent: User guide
nav_order: 5
description: Configure reusable agent personas, tools, models, imports, exports, and AI-assisted drafts.
permalink: /user-guide/automations/sub-agents/
---

# Sub Agents

**Route:** `/automations/agents`

**Product permissions:** `agents.read` to view; `agents.write` to create, edit, enable or disable, delete, import, export, and use AI-assisted drafting.

## Purpose


Sub Agents are saved, reusable agent configurations that constrain persona, instructions, model, and tools. Enabled agents appear in chat selectors and quick-launch surfaces.

## Prerequisites and data sources

Configure an AI provider before using AI draft or enhancement flows. Configure and enable connectors before assigning connector tools. Tool availability is derived from enabled connectors plus registered built-in tools.

## Tabs and actions

The page supports listing, creating, editing, enabling or disabling, deleting, importing, and exporting agents. AI-assisted interview, draft generation, enhancement interview, and enhancement generation require a working model provider. Import validates application configuration; it does not import connector secrets.

## Freshness and scope behavior

The list is application state, not a live Azure inventory. A connector tool can become unavailable when its connector is disabled or removed. Existing agent configuration does not bypass the selected connection, tool classification, product permissions, or Azure permissions.

## Workflow overview

1. Review available models and tools.
2. Create manually or generate a draft from an interview.
3. Restrict tools to the minimum required set.
4. Save and enable the agent.
5. Select it in a new or existing chat and verify its behavior.
6. Export only sanitized configuration when portability is required.

## Interpretation of results

An enabled agent is selectable, not independently authorized. Every run remains subject to user permissions, connection policy, write approval, and downstream service authorization. Validate AI-generated instructions before saving.

## Exports, history, scheduling, and integrations

Single and bulk export endpoints produce portable agent configuration. Imports create application records. Scheduled Tasks can target agent workflows separately and require task permissions.

## Safety and limitations

Agent changes are local application-state writes. They do not directly mutate Azure. Running an agent can invoke read or write tools; write-classified calls remain approval-gated. Exported files must not contain secrets or real environment identifiers before publication.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Agent selector is empty | Enable at least one agent and confirm `agents.read`. |
| A tool is missing | Enable and test its connector, then reopen the editor. |
| Draft generation fails | Test the active AI provider and retry the interview. |
| Import is rejected | Use a supported exported schema and resolve validation errors without adding secrets. |

## Related pages

- [Automations]({{ site.baseurl }}/user-guide/automations/)
- [Connectors]({{ site.baseurl }}/connectors/)
- [How to manage Sub Agents]({{ site.baseurl }}/how-to/automations-connectors/sub-agents/)
