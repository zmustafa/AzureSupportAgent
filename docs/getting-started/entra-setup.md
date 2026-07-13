---
layout: default
title: Microsoft Entra Setup
parent: Getting Started
nav_order: 5
description: Enable Microsoft Graph-backed identity features with deliberately scoped application permissions.
permalink: /getting-started/entra-setup/
---

# Microsoft Entra setup

Microsoft Graph access is optional. Add it when users need Entra users, groups, application registrations, service principals, credential-expiry, MFA, audit-log, role, or Conditional Access context.

Follow the canonical permission list and local-server notes in the [EntraID MCP Server guide]({{ site.baseurl }}/ENTRA_SETUP/). Microsoft permission names and consent requirements can change; verify them in Microsoft documentation before approval.

## Prerequisites

- An application administrator who can configure Graph application permissions.
- A tenant administrator who can grant admin consent.
- An Azure Support Agent connection using the intended managed identity or service principal.
- `settings.write` to enable default assistant tools, or permission to edit the relevant sub-agent.

## Configure Graph access

1. Identify the application identity used by the default Azure connection.
2. In Microsoft Entra, add only the **Application** permissions required by planned features.
3. For read-oriented identity posture, start with the documented read permissions such as directory, user, group, policy, role-management, authentication-method, and audit-log reads.
4. Grant tenant admin consent.
5. Return to Azure Support Agent and open **Settings → EntraID MCP Tools**.
6. Enable the required tools for the default assistant. For a sub-agent, enable Entra tools in that agent's editor instead.
7. Test with a non-sensitive read request and verify the tenant and connection used.

## Write permissions

The existing guide also lists Graph write permissions for group management, password-profile updates, and application management. They are **not required for most queries**. Add them only when an approved workflow needs those operations.

Application-side approval gates do not replace Microsoft Graph least privilege. Both layers must be configured correctly.

## Interpret results

- A successful tool listing confirms that the MCP process loaded; it does not prove every Graph permission is consented.
- A successful read in one area does not imply access to audit logs, authentication methods, or policies.
- Empty results can be legitimate. Compare the requested tenant, time range, and Graph permission before concluding that data is absent.
- Directory results may contain sensitive personal and security information. Handle exports and chat transcripts accordingly.

## Safety

- Never publish tenant IDs, client IDs, secrets, certificates, tokens, user data, or Graph responses.
- Prefer read-only permissions. Separate a write-capable identity if operational segregation is required.
- Review admin consent periodically and remove unused permissions.
- Keep password reset, app-registration mutation, and group mutation behind explicit application permissions and approvals.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Entra tools do not appear | Feature toggle, sub-agent tool selection, MCP process configuration, and application role |
| Graph returns 403 | Exact Application permission, admin consent, and whether the active connection uses the configured identity |
| Authentication-method or audit data is missing | Specialized permissions in the canonical guide; broad directory read may not cover those APIs |
| Data comes from the wrong tenant | Default Azure connection and its tenant/service-principal configuration |
| Local Windows server fails to start | Dedicated Entra MCP virtual environment and configured command/arguments in the canonical guide |

## Related pages

- [Full Entra setup guide]({{ site.baseurl }}/ENTRA_SETUP/)
- [First-run setup]({{ site.baseurl }}/getting-started/first-run/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
