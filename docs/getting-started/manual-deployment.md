---
layout: default
title: Manual Deployment
parent: Getting Started
nav_order: 3
description: Build and deploy the single-container application with explicit control over Azure resources and configuration.
permalink: /getting-started/manual-deployment/
redirect_from:
  - /DEPLOYMENT/
---

# Manual deployment

Use manual deployment when you need to control the image build, registry, Container Apps sizing, storage, networking, or release process. The product ships as one image: FastAPI serves both `/api` and the built SPA, while MCP servers run in-process. No separate frontend, database, or Redis container is required.

Prefer a guided path? Use [one-click installation]({{ site.baseurl }}/getting-started/one-click-install/).

## How it fits in one container

- **SPA** — built by the repository's multi-stage Dockerfile (`VITE_API_BASE=/api`) and copied into `app/static`. FastAPI serves `/assets/*` and falls back to `index.html` for any non-`/api` path, so deep links and refresh work.
- **Database** — PostgreSQL for a shared deployment, or SQLite (`DATABASE_URL=sqlite+aiosqlite:///./.data/app.db`) with `./.data` on an Azure Files volume so it survives revisions.
- **Redis** — not on the request path; omit it.
- **MCP servers** — spawned in-process over stdio. The production image runs its pinned, build-time Azure MCP installation through `azmcp`; the EntraID FastMCP server uses its isolated image virtual environment. The image includes Node 22 and Azure CLI.
- **Azure Resource Graph extension** — production bakes `resource-graph` into `/opt/az-extensions` and sets `AZURE_EXTENSION_DIR` to that stable path, so temporary service-principal CLI sessions do not download an extension into each throwaway `AZURE_CONFIG_DIR`.
- **Dependencies** — pinned in the backend requirements file and installed before `pip install --no-deps .` so every runtime import resolves.

## API prefix

Every backend endpoint is served under `/api` (for example `/api/me`, `/api/chats`, `/api/admin/...`). Only `/healthz` and `/readyz` live at the root. The frontend reads its base from `VITE_API_BASE`, defaulting to `http://localhost:35001/api` for local development. This keeps API routes from colliding with the SPA's client-side routes such as `/inventory`, `/admin`, and `/policy`.

## Azure access modes

| Mode | How |
| --- | --- |
| Your `az login` (default, local) | `~/.azure` is mounted into the backend and your own RBAC applies |
| Service principal | set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET` |
| Managed identity (Container Apps) | assign an identity to the Container App; no secret is needed |

The Azure MCP server starts with `--read-only` (`MCP_READ_ONLY=true`). Write-capable tools are classified, approval-gated, and audited.

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

## Key production environment variables

| Variable | Purpose |
| --- | --- |
| `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` | Bootstrap administrator created on first run. Store the password as a Container App **secret**; the user is forced to change it at first sign-in. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./.data/app.db` on Azure Files, or a PostgreSQL URL (`?ssl=require` for Azure Database for PostgreSQL). |
| `COOKIE_SECURE` | `true` behind HTTPS ingress. |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | Service-principal identity for MCP, or use a managed identity. There is no `~/.azure` mount in Container Apps. |
| `AZURE_EXTENSION_DIR` | Stable Azure CLI extension location. The production image and Bicep deployment use `/opt/az-extensions`, which already contains `resource-graph`. |
| `LLM_API_KEY` | Optional. A model provider key can be configured in Settings instead. |

Keep every secret in a Container App secret or an approved secret store. Never bake one into the image or a template parameter file.

## Deploy from scratch

```pwsh
$RG   = "rg-azsupagent"
$LOC  = "southcentralus"
$ACR  = "azsupagent$((Get-Random -Maximum 99999))"   # globally unique
$APP  = "azsupagent"

az account set --subscription "<subscription-id>"

# 1) Registry (Basic SKU) + cloud build of the single image
az acr create -n $ACR -g $RG --sku Basic --admin-enabled true -l $LOC
az acr build  -r $ACR -t "${APP}:latest" -f Dockerfile .

# 2) Container Apps environment (Consumption)
az containerapp env create -n "$APP-env" -g $RG -l $LOC

# 3) The app: external ingress on 8000, scale-to-zero, admin password as a secret
$server = "$ACR.azurecr.io"
$pw     = az acr credential show -n $ACR --query "passwords[0].value" -o tsv
az containerapp create -n $APP -g $RG `
  --environment "$APP-env" `
  --image "$server/${APP}:latest" `
  --registry-server $server --registry-username $ACR --registry-password $pw `
  --target-port 8000 --ingress external `
  --min-replicas 0 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
  --secrets "admin-password=<your-password>" `
  --env-vars SEED_ADMIN_USERNAME=admin "SEED_ADMIN_PASSWORD=secretref:admin-password" `
             "DATABASE_URL=sqlite+aiosqlite:///./.data/app.db" COOKIE_SECURE=true
```

## Redeploy a new build

```pwsh
az acr build -r $ACR -t "${APP}:latest" -f Dockerfile .
# 'latest' is reused, so force a fresh revision:
az containerapp update -n $APP -g $RG `
  --image "$ACR.azurecr.io/${APP}:latest" --revision-suffix "r$(Get-Random -Maximum 9999)"
```

Prefer a versioned tag or digest over reusing `latest`. See [upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/).

## Cost and scaling

- Lowest-cost posture: Basic ACR plus a Consumption Container App with `--min-replicas 0` (no compute charge while idle) at 0.5 vCPU / 1 GiB. The first request after idle pays a cold start, but the Azure MCP package and Resource Graph extension are already in the image and are not fetched at runtime.
- Use a **single replica** while depending on SQLite or in-container state. Set `--min-replicas 1` to avoid cold starts, at higher cost.

## Production guardrails

- Set `COOKIE_SECURE=true` behind HTTPS.
- Keep the bootstrap password in a platform secret and change it at first sign-in.
- Protect database credentials and the application's secrets-encryption key.
- Use one replica when depending on SQLite or in-memory coordination. A shared database alone does not make every in-memory workflow horizontally scalable.
- Keep Azure MCP read-only unless a reviewed workflow requires writes; product write paths remain permission- and approval-gated.
- Keep the root image's `AZURE_EXTENSION_DIR=/opt/az-extensions` setting. The image bakes the `resource-graph` Azure CLI extension there so temporary service-principal sessions do not install it dynamically.
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
| `az graph` is missing intermittently | Confirm the current image contains `/opt/az-extensions` and the revision sets `AZURE_EXTENSION_DIR` to it; rebuild rather than dynamically installing into each temporary CLI profile |
| First request is slow | Scale-to-zero cold start and initial MCP process startup; production packages are already image-baked |

### Build-time gotchas

- `az acr build`'s log streamer can crash locally on Windows with `UnicodeEncodeError: '\u2713'` (colorama on cp1252). The build still runs server-side; poll `az acr task list-runs -r <acr> --top 1 -o table`, or stream logs to a UTF-8 console without piping or redirecting.
- `az containerapp up --source .` can fail with `'NoneType' object has no attribute 'linux'`. Use the explicit `acr create` → `acr build` → `containerapp create` sequence above instead.
- The Dockerfile copies `backend/` before `pip install` because `setuptools packages=["app"]` validates the package directory at build time.
- The container imports the whole API at startup, so a missing dependency crashes uvicorn immediately. Keep `backend/requirements.txt` complete.
- If `az graph` is reported missing in a service-principal session, verify the revision was built from the root Dockerfile and still sets `AZURE_EXTENSION_DIR=/opt/az-extensions`. Rebuild the image rather than working around it with repeated dynamic installs into temporary config directories.

## Related pages

- [Overview and prerequisites]({{ site.baseurl }}/getting-started/overview/)
- [One-click installation]({{ site.baseurl }}/getting-started/one-click-install/)
- [Upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/)
- [Architecture]({{ site.baseurl }}/technical/architecture/)
