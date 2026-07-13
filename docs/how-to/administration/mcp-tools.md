---
layout: default
title: Inspect Azure and EntraID MCP tools
parent: Administration tasks
grand_parent: How-to guides
nav_order: 62
description: Review live Azure, built-in, and Microsoft Graph tool catalogs and diagnose availability safely.
permalink: /how-to/administration/mcp-tools/
---

# Inspect Azure and EntraID MCP tools

## Prerequisites

- Access to the Admin tool catalog and a running Azure MCP server configuration.
- A working deployment identity or Azure credential chain.
- Entra MCP enabled in General settings.
- A default service-principal Azure connection using a client secret or certificate.
- Microsoft Graph application permissions and administrator consent required by the intended operation.

## Route

- Open `/admin/entratools`.
- Open `/admin/tenants`.
- Open `/admin/tools`.

## How to inspect Azure MCP and built-in tools

1. Review each live Azure MCP tool's name, description, and `read` or `write` classification.
2. Review the built-in utility catalog shown on the same route.
3. Compare the catalog with General settings for MCP read-only mode, built-in enablement, disabled tools, egress policy, command execution, and timeouts.
4. For a required tool, confirm its target Azure connection and least-privilege Azure RBAC.
5. Use the tool in a bounded read-only workflow before considering a write.

**Expected result:** The required tool is visible with an understood classification and policy boundary.

**Verification:** Run one harmless read and confirm its scope and result. A `write` label does not grant permission or automatic execution; approval, connection policy, product permission, and Azure RBAC still apply.

## How to inspect and validate EntraID MCP tools

1. Review server and connection status, then inspect tool name, description, and read/write classification.
2. If the connection is not ready, open `/admin/tenants` and choose **Validate EntraID** on the intended service-principal connection.
3. Review satisfied and missing Microsoft Graph application permissions.
4. Grant only permissions required by the intended read or write, then provide administrator consent.
5. Revalidate the connection and reload `/admin/entratools`.
6. Run a bounded directory read before any approved mutation.

**Expected result:** The Entra page reports an enabled, configured catalog and the intended tool can authenticate to Microsoft Graph.

**Verification:** Confirm the validation report and a narrowly scoped read. Tool visibility does not prove permission to every directory object or authorize a write.

## Safety and rollback

Graph directory writes can have broad impact. Keep safe defaults and approval gates, and never expose client secrets or certificate private material. Remove newly granted Graph consent to roll back excess privilege; disable Entra MCP to remove the integration from runtime use.

Catalog content depends on the connected server version. Keep MCP read-only enabled unless approved writes are required. Roll back exposure by restoring read-only mode or disabling the relevant built-in tool; revoke excess Azure roles separately.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Connection validation is unsupported | Use a service-principal secret or certificate method, not a host-chain or pasted ARM token. |
| Catalog is empty | Check `entra_mcp_enabled`, default connection state, server configuration, and application logs. |
| Validation reports missing permissions | Add only the named Microsoft Graph application permission and admin consent. |
| Catalog is unavailable | Check MCP process/package configuration, network/package access, application logs, and health. |
| Write tool is absent | Confirm whether MCP read-only intentionally filters it. |
| Tool is visible but unauthorized | Grant only the exact Azure role and scope required, then retest. |

## Related docs

- [Azure tenant recipe]({{ site.baseurl }}/how-to/administration/azure-tenants/)
- [Entra setup]({{ site.baseurl }}/ENTRA_SETUP/)
- [MCP tools reference]({{ site.baseurl }}/admin/mcp-tools/)
- [General settings recipe]({{ site.baseurl }}/how-to/administration/general-settings/)
