---
layout: default
title: Administration
nav_order: 20
description: Configure providers, Azure access, integrations, security, references, tools, observability, backups, and demo data.
permalink: /admin/
has_children: true
---

# Administration

Administration is role-gated. Use least privilege: most application configuration requires `settings.write`, while Azure connections, connectors, users, audit, backup, and demo data have dedicated permissions.

![AI provider administration screen]({{ site.baseurl }}/assets/ai-providers.png)

## Configuration

- [AI providers]({{ site.baseurl }}/admin/ai-providers/)
- [Azure tenants and sandbox VMs]({{ site.baseurl }}/admin/azure-tenants-sandbox-vms/)
- [General settings]({{ site.baseurl }}/admin/general-settings/)
- [Connectors]({{ site.baseurl }}/connectors/)

## Security and access

- [Access Control]({{ site.baseurl }}/admin/access-control/)
- [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/)
- [Security documentation]({{ site.baseurl }}/security/)

## Tool preference and references

- [System prompts and scoring]({{ site.baseurl }}/admin/prompts-scoring/)
- [Reference sets and change requests]({{ site.baseurl }}/admin/reference-sets-change-requests/)
- [MCP tools]({{ site.baseurl }}/admin/mcp-tools/)

## Observability and maintenance

- [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/)
- [Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/)

Every visible Settings entry is covered by one of these pages. Changes affect the current tenant/workspace unless a page explicitly describes an Azure connection or external destination.
