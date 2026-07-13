---
layout: default
title: Create and manage Sub Agents
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 5
description: Create, constrain, import, export, enable, and verify reusable Sub Agents.
permalink: /how-to/automations-connectors/sub-agents/
---

# Create and manage Sub Agents

## Prerequisites

- `agents.read` to inspect agents.
- `agents.write` to create, edit, enable or disable, delete, import, or export.
- A tested AI provider for draft and enhancement generation.
- Tested, enabled connectors for connector-backed tools.

## Route

Open `/automations/agents`.

## How to create and verify a Sub Agent

1. Select the create action.
2. Enter a clear name, purpose, and instructions, or complete the AI draft interview.
3. Select a model and only the tools required for the purpose.
4. Review generated instructions against the intended operating boundary.
5. Save and enable the agent.
6. Start a chat, select the agent, and issue a harmless read-only verification prompt.

**Expected result:** The enabled agent appears in the chat picker and responds using its saved configuration.

**Verification:** Confirm the selected agent name in chat and inspect the activity trace for only expected tools.

## How to enhance or disable an agent

1. Open the agent.
2. Edit directly or complete the enhancement interview and generation flow.
3. Review every generated change before saving.
4. Disable the agent when it should no longer be selectable; delete only when the record is no longer needed.

**Expected result:** Saved edits affect subsequent runs; disabled agents disappear from launch selectors.

**Verification:** Reload the agent list and open a new chat to confirm selector behavior.

## How to export and import agents

1. Export one agent or use bulk export.
2. Inspect the file and remove environment identifiers before sharing it.
3. On the target installation, choose import and select the file.
4. Review validation and imported tool references.
5. Save disabled first when connector or provider mappings require review, then enable after a read-only test.

**Expected result:** Valid application configuration is imported without connector credentials.

**Verification:** Open the imported agent, verify model and tool mappings, and run a harmless read-only prompt.

## Safety and rollback

Creation, edits, imports, and enablement change application state only. Disable or delete an unwanted agent to stop new selection. Agent execution does not bypass tool approvals or downstream authorization. Never publish exports containing tenant-specific instructions or identifiers.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Generate action fails | Test the AI provider and simplify the interview input. |
| Imported tools are unavailable | Configure and enable the matching connector, then edit the tool selection. |
| Agent behaves too broadly | Disable it, reduce instructions and tools, then verify with a read-only prompt. |
| User cannot edit | Assign `agents.write`; `agents.read` is intentionally view-only. |

## Related docs

- [Sub Agents reference]({{ site.baseurl }}/user-guide/automations/sub-agents/)
- [Manage connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
