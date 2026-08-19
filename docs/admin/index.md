---
layout: default
title: Administration
nav_order: 20
description: Configure providers, Azure access, integrations, security, references, tools, observability, backups, and demo data.
permalink: /admin/
feature_ids: [SHELL_NAV:admin, ROUTE:admin]
has_children: true
---

# Administration

Administration is capability-gated, not limited to one role name. Readable Settings entries
appear only when the active role has their exact capability. Common splits include
`settings.read`/`settings.write`, `firewall.read`/`firewall.manage`,
`radar.read`/`radar.manage`, `monitor.view` for Usage, and `audit.read` for Audit Log;
connections, connectors, users, backup, and demo data have dedicated permissions. A custom role
that must operate a split editor needs both its route/read key and mutation key. Without the
mutation key, supported sections remain read-only; a mutation key alone does not expose a
read-gated route.

![AI provider administration screen]({{ site.baseurl }}/assets/ai-providers.png)

Looking for numbered procedures rather than reference material? Open the [administration how-to guides]({{ site.baseurl }}/how-to/administration/).

## Configuration

- [AI providers]({{ site.baseurl }}/admin/ai-providers/)
- [Azure tenants]({{ site.baseurl }}/admin/azure-tenants/)
  - [Connect with a service principal (client secret)]({{ site.baseurl }}/admin/azure-tenants-service-principal-secret/)
  - [Connect with a service principal (certificate)]({{ site.baseurl }}/admin/azure-tenants-service-principal-certificate/)
  - [Connect with host identity]({{ site.baseurl }}/admin/azure-tenants-host-identity/)
  - [Connect with a pasted Azure CLI token]({{ site.baseurl }}/admin/azure-tenants-pasted-token/)
- [Sandbox VMs]({{ site.baseurl }}/admin/sandbox-vms/)
- [General settings]({{ site.baseurl }}/admin/general-settings/)
- [Connectors]({{ site.baseurl }}/connectors/)

## Security and access

- [Access Control]({{ site.baseurl }}/admin/access-control/)
- [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/)
- [Network Access]({{ site.baseurl }}/admin/network-access/)
- [Security documentation]({{ site.baseurl }}/security/)

## Tool preference and references

- [System prompts and scoring]({{ site.baseurl }}/admin/prompts-scoring/)
- [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/)
- [MCP tools]({{ site.baseurl }}/admin/mcp-tools/)

## Observability and maintenance

- [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/)
- [Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/)
- [Durable Work Batches]({{ site.baseurl }}/admin/durable-batches/)

## Every Settings entry

The Settings sidebar groups its entries into Configuration, Security & access, Tool Preference, Observability, and Miscellaneous. Each entry below is documented on one of the pages above; where a read and a write capability differ, both are listed.

| Settings entry | Capability | Documented in |
| --- | --- | --- |
| AI Providers | `settings.read` / `settings.write` | [AI providers]({{ site.baseurl }}/admin/ai-providers/) |
| Azure Tenants | `connections.manage` | [Azure tenants]({{ site.baseurl }}/admin/azure-tenants/) |
| Sandbox VMs | `sandbox.exec` | [Sandbox VMs]({{ site.baseurl }}/admin/sandbox-vms/) |
| Connectors | `connectors.manage` | [Connectors]({{ site.baseurl }}/connectors/) |
| General | `settings.read` / `settings.write` | [General settings]({{ site.baseurl }}/admin/general-settings/) |
| Access Control | `users.manage` | [Access Control]({{ site.baseurl }}/admin/access-control/) |
| Security Policy | `users.manage` | [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/) |
| Network Access | `firewall.read` / `firewall.manage` | [Network Access]({{ site.baseurl }}/admin/network-access/) |
| Active Sessions | `users.manage` | [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/) |
| System Prompts | `settings.read` / `settings.write` | [System prompts and scoring]({{ site.baseurl }}/admin/prompts-scoring/) |
| Assessments & Architecture | `settings.read` / `settings.write` | [System prompts and scoring]({{ site.baseurl }}/admin/prompts-scoring/) |
| AMBA Reference Set | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| AMBA Change Requests | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Telemetry Reference Set | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Telemetry Change Requests | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Backup/DR Reference Set | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Backup/DR Change Requests | `coverage.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Retirement Radar Reference | `radar.read` / `radar.manage` | [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Usage | `monitor.view` | [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/) |
| Audit Log | `audit.read` / `settings.write` | [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/) |
| Azure MCP Tools | `settings.read` | [MCP tools]({{ site.baseurl }}/admin/mcp-tools/) |
| EntraID MCP Tools | `settings.read` | [MCP tools]({{ site.baseurl }}/admin/mcp-tools/) |
| Backup & Restore | `backup.manage` | [Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/) |
| Demo Data | `demo.manage` | [Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/) |

Users, Roles, Groups, and Sign-in & SSO are sub-tabs of Access Control and share its `users.manage` capability. [Durable Work Batches]({{ site.baseurl }}/admin/durable-batches/) is a background-execution surface rather than a Settings entry.

Changes affect the current tenant/workspace unless a page explicitly describes an Azure connection or external destination. The live role editor is authoritative when a capability name differs from this table; see the [permissions reference]({{ site.baseurl }}/reference/permissions/).
