---
layout: default
title: Data Flow
parent: Security
nav_order: 1
description: Trace browser, application, Azure, Microsoft Graph, AI provider, MCP, storage, and connector data paths.
permalink: /security/data-flow/
---

# Data flow

1. The browser authenticates to the FastAPI application and sends tenant-scoped API requests.
2. The application resolves the principal's effective roles/permissions and selected Azure connection.
3. Feature services read cached/persisted state or call Azure ARM, Resource Graph, Monitor, Service Health, Reservations, Quotas, or Microsoft Graph through configured credentials/MCP tools.
4. When AI is enabled, the application sends selected prompt/context/tool results to the configured provider and streams output back.
5. Persistent records such as users, sessions, cases, runs, approvals, and audit events are stored by the application; registries and encrypted configuration use the mounted data volume.
6. Explicit connector actions send selected payloads to configured external destinations.

## Boundaries
Tenant ID is applied at database/API boundaries. Azure connections are isolated records, but selecting one intentionally sends requests to that external tenant. AI and connector egress leaves the application boundary; review payload minimization and destination policy. Evidence share tokens create a temporary read path for anyone possessing the token.

## Data minimization
Select the narrowest workload, timespan, fields, and row limits. Avoid sending raw logs to AI or tickets when aggregate evidence is enough. Preserve immutable evidence only for approved retention periods. Disable unused providers, MCP surfaces, and connectors.

## Related pages

- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
