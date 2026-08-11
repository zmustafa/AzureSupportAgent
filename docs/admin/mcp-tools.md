---
layout: default
title: Azure & EntraID MCP Tools
parent: Administration
nav_order: 10
description: Review the live Azure MCP, built-in utility, and Microsoft Graph tool catalogs.
permalink: /admin/mcp-tools/
---

# Azure MCP Tools and EntraID MCP Tools

**Permissions:** `settings.read` to inspect catalogs; `settings.write` to change built-in or Entra tool exposure settings. A custom role using those controls through the UI needs both keys.

## Purpose

**App routes:** `/admin/tools`, `/admin/entratools`

## Prerequisites and data sources

- `settings.read` and a reachable Azure MCP process for the live Azure catalog.
- `settings.read` and a configured default Azure connection for the Entra catalog.
- `settings.write` only when changing built-in utility enablement/disabled tools or
	`entra_mcp_enabled`.
- Azure RBAC or Microsoft Graph application permissions remain separate from product
	permission and are checked when a tool runs.

## Tabs and actions

- `/admin/tools` lists live Azure MCP tools and built-in first-party utilities with `read` or
	`write` classifications. Built-in utilities have a global enable switch and per-tool toggles;
	these settings writes require `settings.write`.
- `/admin/entratools` lists the Entra MCP tool catalog, connection readiness, and required Graph
	application-permission guidance. Exposing Entra tools to the default assistant changes
	`entra_mcp_enabled` and requires `settings.write`.
- Catalog inspection does not execute a listed tool. Tool execution happens later through Chat,
	a sub-agent, or another owning workflow and remains subject to its own product, approval,
	connection, Azure/Graph, egress, and audit controls.

## Freshness and scope behavior

The Azure catalog depends on the connected MCP server and is cached by the server client. The
Entra list depends on local MCP availability and the default connection. A catalog can change
after server/package/configuration changes; reload after changing enablement. Tool visibility
does not prove that the selected connection can authorize every listed operation.

## Workflow overview

### Azure MCP Tools

The page lists the live tool name, description, and `read` or `write` classification from the connected MCP server. It also shows built-in diagnostics/utilities. Catalog content depends on server availability and version; the live page is authoritative.

General settings control `mcp_read_only`, built-in tool enablement/disabled list, egress policy, command execution, and timeouts. A write label means approval and connection policy apply; it is not permission to execute automatically.

### EntraID MCP Tools

The page lists Microsoft Graph tools available from the Entra MCP integration and reports server/connection state. `entra_mcp_enabled` is the master runtime setting. The selected Azure connection still needs the exact Graph application permissions and administrator consent required by a tool.

## Interpretation of results

- `read` and `write` describe the tool's operation class; they are not role grants.
- A missing write tool can be an intentional result of MCP read-only mode.
- A visible Entra tool can still fail if the default connection lacks the exact Graph
	application permission or administrator consent.
- A `settings.read`-only role can inspect the catalog. Any setting update remains backend-gated
	by `settings.write`, even if a control is visible in the current client.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

Keep MCP read-only enabled unless approved write tools are required. Built-in network utilities
are classified read-only and enforce SSRF/egress controls, but they still create outbound network
requests. Entra tools include directory writes when Graph permissions permit them; exposing a
tool does not waive approval or least-privilege requirements. Never copy Graph credentials,
tokens, or certificate private material into documentation or tool prompts.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Server unavailable | Verify process/container configuration, endpoint, logs, and health. |
| Empty catalog | Check enablement and server version; reconnect/restart through the normal local operational process. |
| Tool fails with authorization | Grant only the documented Azure/Graph permission and retest the connection. |
| Write tool absent | `mcp_read_only` may intentionally hide it. |
| Catalog opens but an enable toggle returns forbidden | The active role has `settings.read` but lacks `settings.write`. Switch to an approved role containing both keys. |

## Related pages

- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
- [Access control]({{ site.baseurl }}/security/access-control/)
