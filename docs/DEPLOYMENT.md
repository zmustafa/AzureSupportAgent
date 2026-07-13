---
layout: default
title: Manual Deployment Guide
nav_exclude: true
---

# Manual Deployment Guide (Azure Container Apps)

> Prefer the guided path? See **[One-click installation]({{ site.baseurl }}/getting-started/one-click-install/)**.
> in the README. This guide is for deploying from the CLI with full control.

The whole app — FastAPI **API + the built React SPA + the in-process MCP servers** —
ships as **one image** and runs as a **single Container App**. No separate frontend,
Postgres, or Redis containers are required.

## How it fits in one container

- **SPA**: built by the repository's multi-stage Dockerfile (`VITE_API_BASE=/api`) and
  copied into `app/static`. FastAPI serves `/assets/*` and falls back to `index.html` for
  any non-`/api` path, so deep links and refresh work.
- **Database**: SQLite (`DATABASE_URL=sqlite+aiosqlite:///./.data/app.db`). Put `./.data`
  on an Azure Files volume to persist across revisions. Postgres is still supported by
  pointing `DATABASE_URL` at it.
- **Redis**: not on the request path; omit it.
- **MCP servers**: spawned in-process over stdio (`npx @azure/mcp`, EntraID FastMCP) — the
  image already includes Node 20 + Azure CLI.
- **Dependencies**: pinned in the backend requirements file
  (frozen from a working environment, Windows-only packages removed) and installed before
  `pip install --no-deps .` so every runtime import resolves.

## API prefix

Every backend endpoint is served under **`/api`** (e.g. `/api/me`, `/api/chats`,
`/api/admin/...`). Only `/healthz` and `/readyz` live at the root. The frontend reads its
base from `VITE_API_BASE` (default `http://localhost:8000/api` for local dev). This keeps
API routes from colliding with the SPA's client-side routes (`/inventory`, `/admin`,
`/policy`, …) so the single-container build can serve the app at every non-`/api` path.

## Azure access modes

| Mode | How |
|------|-----|
| Your `az login` (default, local) | `~/.azure` is mounted into the backend; uses your RBAC |
| Service principal | set `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` |
| Managed identity (ACA) | assign an identity to the Container App; no secret needed |

The MCP server starts with `--read-only` (`MCP_READ_ONLY=true`). Write-capable tools are
classified, approval-gated, and audited.

## Key env vars (production)

| Variable | Purpose |
|----------|---------|
| `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` | Bootstrap admin created on first run (store the password as a Container App **secret**; the user is forced to change it on first login) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./.data/app.db` (Azure Files) or a Postgres URL (`?ssl=require` for Azure PostgreSQL) |
| `COOKIE_SECURE` | `true` (HTTPS ingress) |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | Service-principal identity for MCP (or use a managed identity) — there is no `~/.azure` mount in ACA |
| `LLM_API_KEY` (or configure in Settings) | LLM provider key |

## Deploy from scratch (PowerShell)

```pwsh
$RG   = "rg-azsupagent"
$LOC  = "southcentralus"
$ACR  = "azsupagent$((Get-Random -Maximum 99999))"   # globally-unique
$APP  = "azsupagent"

az account set --subscription "<subscription-id>"

# 1) Registry (cheapest Basic SKU) + cloud build of the single image
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

## Cost & scaling notes

- **Cheapest** posture: Basic ACR + Consumption Container App, `min-replicas 0`
  (scale-to-zero → no compute charge when idle), `0.5 vCPU / 1 GiB`. The first request
  after idle pays a cold-start (plus a one-time `npx @azure/mcp` fetch).
- **Single replica only** while using SQLite or in-container state (it's stateful). Set
  `--min-replicas 1` to avoid cold starts (costs more).

## Gotchas learned (Windows + ACR)

- **`az acr build` log streamer crashes** locally on Windows with
  `UnicodeEncodeError: '\u2713'` (colorama → cp1252). The build still runs server-side.
  Poll `az acr task list-runs -r <acr> --top 1 -o table` for status, or stream logs
  straight to a UTF-8 console (don't pipe/redirect).
- **`az containerapp up --source .`** can fail with
  `'NoneType' object has no attribute 'linux'`; use the explicit `acr create` →
  `acr build` → `containerapp create` flow above instead.
- The Dockerfile **copies `backend/` before `pip install`** because
  `setuptools packages=["app"]` validates the package dir at build time.
- The container imports the whole API at startup, so a **missing dependency crashes
  uvicorn immediately** — keep `backend/requirements.txt` complete.

## Notes & limitations (current build)

- Multi-session chat, SSE streaming, persistence, and end-to-end read-only Azure MCP tool
  calling are complete.
- Write actions are classified and gated; execution flows through the approval workflow.
- Single-replica Postgres/SQLite (stateful). For Container Apps, back state with Azure
  Files volumes; everything else is stateless and scales horizontally.
