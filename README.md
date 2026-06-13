# Azure Support Agent

An enterprise, multi-session chat web app where each conversation has isolated
context. The backend orchestrator drives an LLM that can call the **Azure MCP
server** to investigate and troubleshoot problems across an Azure subscription
(networking, compute, storage, identity, configuration, health, and more).
Includes an admin dashboard (MCP tools, usage, audit).

See [docs/TECHNICAL_SPEC.md](docs/TECHNICAL_SPEC.md) for the full design.

## Deploy to Azure (one-click)

> **Status: tested.** The template provisions a managed PostgreSQL database,
> Azure Files state storage, and the Container App running the public image.

The goal: click a button, and the app runs in **your** Azure tenant with a managed
database — no CLI, no manual wiring.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fzmustafa%2FAzureSupportAgent%2Fmain%2Fdeploy%2Fmain.json)

What the button will provision (in your subscription, one deployment):

1. **Azure Container App** running the public Docker Hub image
2. **Azure Database for PostgreSQL — Flexible Server** (managed), auto-linked via `DATABASE_URL`
3. **Azure Files** share mounted at `/app/.data` (registries, caches, encryption key)
4. **Container Apps environment** + external HTTPS ingress on port 8000

You supply only an **admin password** (first login); then connect your Azure tenant and
an LLM from **Settings** — the AI does the rest (workload discovery, architectures,
coverage scans, assessments, retirement radar, performance profiling).

The first admin login is forced to set a new password.

Repo: <https://github.com/zmustafa/AzureSupportAgent>

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Redis, SSE streaming
- **Agent:** pluggable LLM provider (OpenAI first) + tool-calling loop
- **Azure access:** official Azure MCP server (`@azure/mcp`) spawned over **stdio**
- **Entra ID access:** vendored **EntraID MCP Server** (Microsoft Graph, FastMCP) spawned over **stdio**
- **Frontend:** React 18, TypeScript, Vite, Tailwind
- **Hosting target:** Azure Container Apps only (local dev via docker-compose)

## Key features

- **Multi-session chat** with isolated context, live SSE streaming, and a per-message
  activity feed (reasoning steps + tool calls) that persists across reloads.
- **Deep investigation ("War Room")** — toggle deep mode to dispatch specialist agents
  that form hypotheses and validate them against live Azure evidence. A War Room badge
  marks deep chats in the sidebar, and an animated agent icon shows at the top while an
  investigation runs.
- **Stop control** — agent turns run as background tasks decoupled from the SSE
  connection (so navigating away doesn't kill the work). The Stop button cancels the
  server-side turn and persists whatever was produced so far.
- **Sandbox VMs** — onboard dedicated SSH VMs (Settings → Sandbox VMs) that sit inside a
  workload's network; the agent runs diagnostic commands on them via `vm_exec` to reach
  private endpoints, in normal and deep chat.
- **Pluggable LLM providers** — OpenAI, Azure OpenAI, GitHub Copilot/Models, Ollama, and
  ChatGPT (OAuth), configurable at runtime from the admin dashboard.
- **Admin dashboard** — MCP tool catalog, usage, audit log, AI provider config, and more.

## How it connects (local)

```
Browser ─► frontend (5173) ─► backend (8000) ─┬─► OpenAI  (LLM, via API key)
                                               └─► Azure MCP (npx, stdio)
                                                        └─► your Azure subscription
                                                            (DefaultAzureCredential
                                                             = your `az login`)
```

Nothing is deployed to Azure for local dev. The MCP server reaches your real
subscription **outbound** using your signed-in identity and existing RBAC. It runs
**read-only** by default.

## Prerequisites

- Docker Desktop
- Azure CLI (`az`) — run `az login` once and select your target subscription
- An OpenAI API key (or Azure OpenAI endpoint + key)

## Quick start

1. Sign in to Azure and pick the subscription to troubleshoot:
   ```pwsh
   az login
   az account set --subscription "<your-subscription-id>"
   ```
2. Create your env file and fill in the LLM key:
   ```pwsh
   Copy-Item .env.example .env
   # edit .env: set LLM_API_KEY (and AZURE_SUBSCRIPTION_ID if you want to pin one)
   ```
3. Start everything:
   ```pwsh
   docker compose up --build
   ```
4. Open the app: http://localhost:5173

The backend runs DB migrations on startup. First MCP call downloads `@azure/mcp`
via `npx` (a few seconds), then it's cached.

### Health checks

- Backend: http://localhost:8000/healthz
- Identity (dev): http://localhost:8000/me
- MCP tools (admin): http://localhost:8000/admin/mcp/tools

## EntraID (Microsoft Graph) MCP Server

In addition to the Azure MCP server, the app integrates the **EntraID MCP Server**. It
exposes Microsoft Graph tools for Entra ID (Azure AD): users, groups, app registrations
& service principals, **secret/certificate expiry**, MFA status, sign-in & audit logs,
and conditional-access policies. It is spawned over stdio just like the Azure MCP server
and its tools flow into the same provider tool-calling loop (works with every LLM
provider, including the Copilot/Codex guided-tool-calling adapters).

- **Enable for the default assistant:** Settings → **EntraID MCP Tools** → toggle on.
- **Per sub agent:** check *"Also allow all EntraID (Microsoft Graph) tools (MCP)"*
  in the agent editor (next to the Azure tools checkbox).
- **Identity:** it authenticates to Graph using the **default Azure connection's**
  service-principal credentials (tenant id / client id / client secret, or certificate).
- **Tools listing (admin):** http://localhost:8000/admin/entra/tools

### Required Microsoft Graph permissions

Grant these **Application** permissions to the app registration used by the connection,
then grant admin consent:

| API / Permission | Type | Description |
| --- | --- | --- |
| `AuditLog.Read.All` | Application | Read all audit log data |
| `AuthenticationContext.Read.All` | Application | Read all authentication context information |
| `DeviceManagementManagedDevices.Read.All` | Application | Read Microsoft Intune devices |
| `Directory.Read.All` | Application | Read directory data |
| `Group.Read.All` | Application | Read all groups |
| `GroupMember.Read.All` | Application | Read all group memberships |
| `Group.ReadWrite.All` | Application | Create, update, delete groups; manage group members and owners |
| `Policy.Read.All` | Application | Read your organization's policies |
| `RoleManagement.Read.Directory` | Application | Read all directory RBAC settings |
| `User.Read.All` | Application | Read all users' full profiles |
| `User-PasswordProfile.ReadWrite.All` | Application | Least privileged permission to update the passwordProfile property |
| `UserAuthenticationMethod.Read.All` | Application | Read all users' authentication methods |
| `Application.ReadWrite.All` | Application | Create, update, and delete applications (app registrations) and service principals |

Read-only permissions are sufficient for most queries; the `*.ReadWrite.All` permissions
enable group, password, and application management. Write tools (create/update/delete/
reset) are gated behind the app's approval policy.

### Local setup notes

The Graph SDK has very deep file paths, so its dependencies live in a dedicated venv to
avoid the Windows 260-char path limit (Windows long-path support is also enabled). The
backend spawns the server using `ENTRA_MCP_COMMAND` (that venv's python) and
`ENTRA_MCP_ARGS` (the stdio launcher `third_party/entraid-mcp-server/run_server.py`).
Override these via environment variables if your paths differ.

## Auth (local dev)

`DEV_AUTH=true` injects a fake admin identity so you can use the chat and admin
dashboard without standing up Keycloak. Set `DEV_AUTH_ROLE=user` to test the
non-admin view. Real OIDC (Keycloak) is a later phase.

## Azure access modes

| Mode | How |
|------|-----|
| Your `az login` (default) | `~/.azure` is mounted into the backend; uses your RBAC |
| Service principal | set `AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET` in `.env` |

The MCP server starts with `--read-only` (`MCP_READ_ONLY=true`). Gated-write
execution + approval workflow is Phase 3; the approval data model and admin
approvals API are already present.

## Project layout

```
backend/    FastAPI app (api, agent, mcp, core, models, schemas) + alembic
frontend/   React + Vite SPA (chat + admin); animated agent icons in public/agent-icons
docs/       Technical specification
docker-compose.yml
```

## Running backend tests / dev outside Docker (optional)

```pwsh
cd backend
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# requires local postgres/redis + node + az login
uvicorn app.main:app --reload
```

> **API prefix:** every backend endpoint is served under **`/api`** (e.g.
> `/api/me`, `/api/chats`, `/api/admin/...`). Only `/healthz` and `/readyz` live at the
> root. The frontend reads its base from `VITE_API_BASE` (default `http://localhost:8000/api`
> for local dev). This keeps API routes from colliding with the SPA's client-side routes
> (`/inventory`, `/admin`, `/policy`, …) so the single-container build can serve the app
> at every non-`/api` path.

## Single-container deployment (Azure Container Apps)

The whole app — FastAPI **API + the built React SPA + the in-process MCP servers** —
ships as **one image** and runs as a **single Container App**. No separate frontend,
Postgres, or Redis containers are required.

How it fits in one container:

- **SPA**: built by the multi-stage [`Dockerfile`](Dockerfile) (`VITE_API_BASE=/api`) and
  copied into `app/static`. FastAPI serves `/assets/*` and falls back to `index.html` for
  any non-`/api` path, so deep links and refresh work ([app/main.py](backend/app/main.py)).
- **Database**: SQLite (`DATABASE_URL=sqlite+aiosqlite:///./.data/app.db`). Put `./.data`
  on an Azure Files volume to persist across revisions. Postgres is still supported by
  pointing `DATABASE_URL` at it.
- **Redis**: not on the request path; omit it.
- **MCP servers**: spawned in-process over stdio (`npx @azure/mcp`, EntraID FastMCP) — the
  image already includes Node 20 + Azure CLI.
- **Dependencies**: pinned in [`backend/requirements.txt`](backend/requirements.txt)
  (frozen from a working environment, Windows-only packages removed) and installed before
  `pip install --no-deps .` so every runtime import resolves.

### Key env vars (production)

| Variable | Purpose |
|----------|---------|
| `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` | Bootstrap admin created on first run (store the password as a Container App **secret**) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./.data/app.db` (Azure Files) or a Postgres URL |
| `COOKIE_SECURE` | `true` (HTTPS ingress) |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | Service-principal identity for MCP (or use a managed identity) — there is no `~/.azure` mount in ACA |
| `LLM_API_KEY` (or configure in Settings) | LLM provider key |

### Deploy from scratch (PowerShell)

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

### Redeploy a new build

```pwsh
az acr build -r $ACR -t "${APP}:latest" -f Dockerfile .
# 'latest' is reused, so force a fresh revision:
az containerapp update -n $APP -g $RG `
  --image "$ACR.azurecr.io/${APP}:latest" --revision-suffix "r$(Get-Random -Maximum 9999)"
```

### Cost & scaling notes

- **Cheapest** posture: Basic ACR + Consumption Container App, `min-replicas 0`
  (scale-to-zero → no compute charge when idle), `0.5 vCPU / 1 GiB`. The first request
  after idle pays a cold-start (plus a one-time `npx @azure/mcp` fetch).
- **Single replica only** while using SQLite or in-container state (it's stateful). Set
  `--min-replicas 1` to avoid cold starts (costs more).

### Gotchas learned (Windows + ACR)

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

- Phase 0–2: multi-session chat, SSE streaming, persistence, and end-to-end
  read-only Azure MCP tool calling.
- Write actions are classified and gated but not executed yet (Phase 3).
- Single-replica Postgres/Redis (stateful). For Container Apps, back them with
  Azure Files volumes; everything else is stateless and scales horizontally.
