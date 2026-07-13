---
layout: default
title: Azure & EntraID MCP Tools
parent: Administration
nav_order: 9
description: Review the live Azure MCP, built-in utility, and Microsoft Graph tool catalogs.
permalink: /admin/mcp-tools/
---

# Azure MCP Tools and EntraID MCP Tools

**Permissions:** `settings.read` to inspect settings; administrative configuration requires `settings.write`

## Purpose

**App routes:** `/admin/tools`, `/admin/entratools`

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Azure MCP Tools

The page lists the live tool name, description, and `read` or `write` classification from the connected MCP server. It also shows built-in diagnostics/utilities. Catalog content depends on server availability and version; the live page is authoritative.

General settings control `mcp_read_only`, built-in tool enablement/disabled list, egress policy, command execution, and timeouts. A write label means approval and connection policy apply; it is not permission to execute automatically.

### EntraID MCP Tools

The page lists Microsoft Graph tools available from the Entra MCP integration and reports server/connection state. `entra_mcp_enabled` is the master runtime setting. The selected Azure connection still needs the exact Graph application permissions and administrator consent required by a tool.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Server unavailable | Verify process/container configuration, endpoint, logs, and health. |
| Empty catalog | Check enablement and server version; reconnect/restart through the normal local operational process. |
| Tool fails with authorization | Grant only the documented Azure/Graph permission and retest the connection. |
| Write tool absent | `mcp_read_only` may intentionally hide it. |

## Related pages

### Related docs

- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
- [Access control]({{ site.baseurl }}/security/access-control/)
