---
layout: default
title: Manual Deployment
parent: Getting Started
nav_order: 3
description: Build and deploy the single-container application with explicit control over Azure resources and configuration.
permalink: /getting-started/manual-deployment/
---

# Manual deployment

Use manual deployment when you need to control the image build, registry, Container Apps sizing, storage, networking, or release process. The product ships as one image: FastAPI serves both `/api` and the built SPA, while MCP servers run in-process.

The supported command sequence, production variables, and platform notes are maintained in the [Manual Deployment Guide]({{ site.baseurl }}/DEPLOYMENT/). Follow that guide rather than copying commands from third-party posts.

## Prerequisites

- Azure CLI authenticated to the intended subscription.
- Permission to create a resource group, Azure Container Registry, Container Apps resources, identities, role assignments, storage, and the chosen database.
- Dockerfile build context from a trusted release or reviewed source checkout.
- A production secret-management plan.

## Deployment workflow

1. **Choose persistence.** Use PostgreSQL for a shared production database, or place SQLite's `.data` directory on Azure Files. Never rely on an ephemeral container filesystem.
2. **Choose the identity.** Prefer a Container App managed identity. If using a service principal, store its secret or certificate as a Container App secret.
3. **Build and tag the image.** Build from the repository root so the frontend and backend are included. Prefer an immutable release tag over relying only on `latest`.
4. **Create the Container Apps environment and application.** Expose port 8000 through HTTPS ingress.
5. **Set production configuration.** Important settings include the database URL, secure-cookie behavior, bootstrap administrator values, public URL, connection identity, and optional model configuration.
6. **Attach persistent storage** before allowing production traffic when SQLite is selected.
7. **Verify health.** Check `/healthz` for liveness and `/readyz` for readiness, then load the SPA through the public URL.
8. **Grant Reader** to the application identity at the intended Azure scope.
9. Complete [First-run setup]({{ site.baseurl }}/getting-started/first-run/).

## Production guardrails

- Set `COOKIE_SECURE=true` behind HTTPS.
- Keep the bootstrap password in a platform secret and change it at first sign-in.
- Protect database credentials and the application's secrets-encryption key.
- Use one replica when depending on SQLite or in-memory coordination. A shared database alone does not make every in-memory workflow horizontally scalable.
- Keep Azure MCP read-only unless a reviewed workflow requires writes; product write paths remain permission- and approval-gated.
- Restrict ingress and outbound traffic deliberately if private networking is required.

## Validate the result

- The Container App revision is healthy and serving the expected immutable image.
- `/healthz` and `/readyz` succeed.
- Refreshing a client-side route such as `/workloads` returns the SPA, not a 404.
- Database data survives a revision restart.
- The managed identity or service principal can list only the intended Azure scopes.
- No credentials appear in revision logs or environment-variable exports.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Container exits at startup | Application logs, dependency installation, database URL, and mounted paths |
| SPA loads but API calls fail | `/api` routing, frontend build-time API base, ingress, and CORS/public URL settings |
| Data disappears after a revision | Azure Files mount or PostgreSQL connection; SQLite must not live only in the image filesystem |
| Deep links return 404 | Requests must reach FastAPI's SPA fallback, not a static host without rewrite rules |
| Azure tools cannot authenticate | Managed-identity assignment or service-principal variables and RBAC scope |
| First request is slow | Scale-to-zero cold start and initial MCP process/package startup |

## Related pages

- [Canonical manual deployment guide]({{ site.baseurl }}/DEPLOYMENT/)
- [Overview and prerequisites]({{ site.baseurl }}/getting-started/overview/)
- [Upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/)
