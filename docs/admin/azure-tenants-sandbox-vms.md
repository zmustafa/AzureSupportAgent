---
layout: default
title: Azure Tenants & Sandbox VMs
parent: Administration
nav_order: 2
description: Manage encrypted Azure connections and approved troubleshooting VMs.
permalink: /admin/azure-tenants-sandbox-vms/
---

# Azure Tenants and Sandbox VMs

**Permissions:** `connections.manage`; `settings.write` for sandbox configuration; `sandbox.exec` to execute diagnostics

## Purpose

**App routes:** `/admin/tenants`, `/admin/sandboxvms`

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Azure Tenants

A connection stores a display name, tenant/client identifiers, authentication material, default scope metadata, disabled/default state, and **Read only** policy. Depending on the build and deployment identity, the UI presents supported credential methods. Use the form as the source of truth rather than copying credentials into documentation.

1. Create the connection with a non-sensitive display name.
2. Enter the exact tenant/client information and secret/certificate or select the supported identity method.
3. Keep **Read only** enabled initially.
4. Run the staged connection test and discover subscriptions/management groups.
5. Validate Graph capability only when Entra features require it.
6. Set one connection as default; feature scope pickers can still select another enabled connection.

Only one connection is default. Disabled connections remain stored but are unavailable to normal workflows. Credential values are encrypted and masked.

### Sandbox VMs

Register only dedicated diagnostic hosts. The form includes display name, Azure resource ID, owning connection, SSH host/port/user, supported authentication method, and enabled state. Prefer short-lived or managed credentials and network restriction.

A connectivity test performs a small identity probe. Agent execution is separately gated by `sandbox.exec`, General settings, command timeout, and read-only/write controls. Auto-installing missing tools changes the VM and should remain disabled unless explicitly approved.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Connection auth failure | verify tenant, client, credential validity, consent, authority/endpoint, and clock. |
| Feature is half-blind | open Connection Capability and grant only the missing read permission. |
| VM unreachable | check DNS, route/firewall/NSG, port, username, key format, and container source network. |
| Command times out | narrow the command and review `sandbox_command_timeout_seconds`. |

## Related pages

- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
