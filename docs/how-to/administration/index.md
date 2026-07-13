---
layout: default
title: Administration tasks
parent: How-to guides
nav_order: 5
description: Task-oriented procedures for every visible administration area.
permalink: /how-to/administration/
has_children: true
---

# Administration tasks

Use these procedures to configure the application, govern access, maintain reference data, and verify administrative changes without exposing credentials.

## How to choose an administration task

### Prerequisites

- Sign in and confirm that the required product permission is present.
- Record the current state before changing settings.
- Keep credential values in an approved secret store, not in tickets, screenshots, or documentation.

### Frontend Route

Open `/admin`, then select the required administration card.

### Steps

1. Choose the page that matches the intended outcome:
   - [Configure AI providers]({{ site.baseurl }}/how-to/administration/ai-providers/)
   - [Manage Azure tenants]({{ site.baseurl }}/how-to/administration/azure-tenants/)
   - [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/)
   - [Configure connectors]({{ site.baseurl }}/how-to/administration/connectors/)
   - [Change General settings]({{ site.baseurl }}/how-to/administration/general-settings/)
   - [Manage users, roles, groups, and SSO]({{ site.baseurl }}/how-to/administration/access-control/)
   - [Set policy and revoke sessions]({{ site.baseurl }}/how-to/administration/security-sessions/)
   - [Edit prompts and scoring]({{ site.baseurl }}/how-to/administration/prompts-scoring/)
   - [Maintain reference sets and requests]({{ site.baseurl }}/how-to/administration/reference-sets/)
   - [Review usage and audit history]({{ site.baseurl }}/how-to/administration/usage-audit/)
   - [Inspect Azure and EntraID MCP tools]({{ site.baseurl }}/how-to/administration/mcp-tools/)
   - [Back up, restore, or manage demo data]({{ site.baseurl }}/how-to/administration/backup-demo/)
2. Follow the exact route, permission, verification, and rollback guidance on that page.
3. Review the Audit Log after a write.

### Expected result

The selected task opens at the correct frontend route and the operator knows its permission and safety boundary before making a change.

### Verification

Confirm the route in the browser and verify that the page heading matches the intended admin area.

### Safety and rollback

Prefer disable, preview, test, and reset controls over deletion. Secrets are masked or write-only on supported forms: a stored value is not evidence that it remains valid. Take a backup before broad changes.

### Troubleshooting

| Symptom | Resolution |
| --- | --- |
| A card or action is absent | Confirm the signed-in user's effective permission and active role. |
| A save is rejected | Review inline validation and change only visible, supported fields. |
| A credential field is blank while editing | This commonly represents a stored write-only value; do not paste it into logs. Enter a new value only to rotate it. |

### Related docs

- [Administration reference]({{ site.baseurl }}/admin/)
- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
