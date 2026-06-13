# Azure Support Agent - Technical Specification

> Status: Implementation-current specification
> Date: 2026-06-10

## 1. Product Summary

Azure Support Agent is an enterprise Azure operations and support application. It combines
multi-session chat, LLM-driven investigations, Azure and Entra tooling, workload discovery,
inventory, assessments, architecture diagrams, architecture memory, workbooks, automations,
policy governance, notifications, and customizable Monitor dashboards.

The product started as an Azure troubleshooting chat agent. The current codebase is now a
broader Azure operations workbench with these major surfaces:

- Multi-session chat with isolated context, streaming turns, tool timelines, suggestions,
  images, and deep investigations.
- Admin-managed LLM provider configuration with multiple providers and live model refresh.
- Azure and Entra ID tool access through MCP servers and built-in first-party utilities.
- Azure connections registry with encrypted credentials and per-connection governance.
- Azure Workloads, Inventory, Assessments, Policy, Architectures, Architecture Memory,
  Workbooks, Playbooks, Automations, Notifications, and Monitor dashboards.
- Monitor 2.0: customizable dashboards with widgets, AI authoring, per-widget data sources,
  web/TCP ping history, and AI-generated workload dashboards that use Architecture Memory.

## 2. Current Stack

| Layer | Current implementation |
| --- | --- |
| Backend | Python 3.12, FastAPI, async SQLAlchemy 2, Pydantic v2, Alembic, SSE via sse-starlette |
| Local DB | SQLite with aiosqlite under `backend/.data/app.db` |
| Production DB target | PostgreSQL container in Azure Container Apps environment |
| Cache/session target | Redis container, optional or bypassed in native local dev paths |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, TanStack Query, React Router |
| Visualization | Recharts, react-grid-layout, Mermaid, XYFlow, TopoJSON/world-atlas |
| AI | Provider abstraction with streaming and normalized tool calls |
| Azure tools | Azure MCP server over stdio, Azure CLI/Resource Graph command runner |
| Entra tools | Vendored EntraID MCP server over stdio, Microsoft Graph permissions |
| Hosting target | Azure Container Apps only |
| Local dev | Native backend/frontend or Docker Compose |

## 3. Repository Layout

```text
aznetagent/
  README.md
  docker-compose.yml
  .env.example
  docs/
    TECHNICAL_SPEC.md
  backend/
    pyproject.toml
    Dockerfile
    alembic.ini
    alembic/
    app/
      api/
      agent/
      architectures/
      assessments/
      auth/
      automations/
      azure/
      connectors/
      core/
      exec/
      inventory/
      mcp/
      models/
      monitor/
      notifications/
      playbooks/
      policy/
      schemas/
      workbooks/
      workloads/
  frontend/
    package.json
    Dockerfile
    src/
      api.ts
      App.tsx
      components/
      components/monitor/
  third_party/
    entraid-mcp-server/
```

## 4. Backend Architecture

The backend is a modular FastAPI application. Routers live under `app/api`, feature
registries and business logic live in feature packages, and durable relational data uses
SQLAlchemy ORM models.

### 4.1 API Routers

| Router | Responsibility |
| --- | --- |
| `api/chats.py` | Chat CRUD, streaming message turns, suggestions, executable command stream, deep-investigation listing |
| `api/admin.py` | App settings, AI prompts, LLM config/test/model refresh, tool catalogs, approvals, audit, usage, Monitor endpoints |
| `api/connections.py` | Admin-managed Azure tenant connections and live connection tests |
| `api/connectors.py` | External connector registry and test calls |
| `api/workloads.py` | Workload CRUD, discovery, prefetch, tree/search/facet APIs |
| `api/inventory.py` | Inventory grid, map facets, cost/refresh metadata, AI inventory search |
| `api/assessments.py` | Assessment portfolio, runs, custom controls, waivers, finding lifecycle, tickets |
| `api/architectures.py` | Architecture CRUD, AI reverse-engineering, collections, memory APIs, revisions |
| `api/policy.py` | Policy inventory, compliance, baselines, AI advisors |
| `api/workbooks.py` | Workbook CRUD, import/export, preview/run/history, workbook tiles |
| `api/playbooks.py` | Chained workbook playbooks and portability |
| `api/automations.py` | Custom agents, scheduled tasks, scheduler targets, manual task execution |
| `api/notifications.py` | In-app notifications, delivery state, notification rules |
| `api/users.py` | Users, roles, groups, sessions, identity providers |

### 4.2 Agent and LLM Layer

`app/agent` contains the provider abstraction and the agent execution loop.

Key modules:

- `provider.py`: `LLMProvider` interface and streaming event contract.
- `factory.py`: builds the active provider from runtime configuration.
- `orchestrator.py`: main tool-calling loop for chat turns.
- `turn_runner.py`: disconnect-resilient turn execution and live turn snapshots.
- `deep_investigation.py`: multi-phase investigation and war-room workflow.
- `deep_agents.py`: domain-specific investigation agents.
- `prompts.py`: system prompts, starter suggestions, clarification prompts.
- `builtins.py`: built-in utility tools such as web fetch, HTTP request, DNS lookup,
  TCP port check, ping, and traceroute, all with SSRF and egress controls.

Supported provider families include:

- OpenAI-compatible providers: OpenAI, Azure OpenAI, GitHub Models, Ollama, Grok,
  Mistral, Gemini, OpenRouter, LM Studio.
- Anthropic Claude native Messages API.
- GitHub Copilot web-chat-thread protocol.
- ChatGPT/Codex OAuth Responses API.

Provider configuration is admin-editable at runtime via `app/core/llm_config.py` and
`/admin/llm/*` endpoints. Changes do not require a backend restart.

### 4.3 MCP and Tool Access

`app/mcp/client.py` spawns and manages MCP tool catalogs.

- Azure MCP server is launched over stdio using `npx @azure/mcp server start --transport stdio`.
- EntraID MCP server is vendored under `third_party/entraid-mcp-server` and also runs over stdio.
- Tool catalogs are cached and classified as read/write.
- Write-capable tools are gated by approval policy and audit logging.
- Built-in first-party tools are not MCP tools, but are exposed through the same tool-call loop.

Azure CLI and Resource Graph execution is handled by `app/exec/command_runner.py`.
It validates commands, rejects shell operators, classifies destructive verbs, supports
service-principal session reuse, and captures output with bounded size.

### 4.4 Core Services

| Module | Responsibility |
| --- | --- |
| `core/config.py` | Environment-backed application settings |
| `core/db.py` | Async SQLAlchemy engine/session and SQLite path resolution |
| `core/security.py` | Principal resolution, auth dependencies, admin enforcement |
| `core/app_settings.py` | Runtime app settings and built-in tool toggles |
| `core/llm_config.py` | Runtime LLM provider registry |
| `core/azure_connections.py` | Azure connection registry with encrypted secrets |
| `core/ai_prompts.py` | Mutable prompt catalog |
| `core/crypto.py` | Fernet encryption helpers |
| `core/siem_export.py` | Audit export integration |
| `core/pricing.py` | Token usage pricing helpers |

## 5. Frontend Architecture

The frontend is a React/Vite SPA. `ChatView.tsx` is the primary shell and switches
between chat and product sections based on route.

### 5.1 Main Routes

| Route | Surface |
| --- | --- |
| `/dashboard` | Overview and entry point |
| `/`, `/chat`, `/c/:chatId` | Multi-session chat |
| `/workloads`, `/workloads/:id` | Azure workload browser and editor |
| `/inventory`, `/inventory/:tab` | Inventory, cost, location map, filters |
| `/assessments`, `/assessments/:id` | Well-Architected assessments and findings |
| `/architectures`, `/architectures/:id/memory` | Architecture diagram editor and memory |
| `/policy`, `/policy/:tab` | Azure Policy inventory and compliance |
| `/workbooks` | Workbook authoring and runs |
| `/automations`, `/automations/:section` | Custom agents and scheduled tasks |
| `/monitor` | Monitor 2.0 dashboards |
| `/notifications` | Notification center |
| `/admin`, `/admin/:section` | Admin dashboard |

### 5.2 Key Components

- `ChatView.tsx`: main shell, sidebar, routing, chat streaming, deep investigation UI.
- `AdminView.tsx`: settings, LLM providers, tools, approvals, usage, audit, connections.
- `WorkloadsView.tsx`: workload registry, discovery, Autopilot-style build flow.
- `InventoryView.tsx` and `InventoryLocationMap.tsx`: inventory, cost, location map,
  real tiles, region/resource-group/resource-type/subscription filters.
- `AssessmentsView.tsx`: WAF assessment runs, finding tables, waivers, tickets.
- `ArchitecturesView.tsx`: architecture diagram canvas, workload linking, rebuild from workload.
- `ArchitectureMemoryView.tsx`: architecture memory editor with versioning and markdown preview.
- `WorkbooksView.tsx`: reusable workbook commands/queries, AI design, test runs.
- `AutomationsView.tsx`: custom agents, scheduled tasks, connector tools.
- `MonitorView.tsx`: customizable Monitor dashboards.
- `components/monitor/widgets.tsx`: generic widget renderers for chart/table/stat/gauge/map/etc.
- `components/monitor/editor.tsx`: widget editor, AI widget modal, build-from-workload modal.

`frontend/src/api.ts` is the centralized typed API client.

## 6. Persistence Model

The application uses two storage patterns.

### 6.1 Relational Database

SQLAlchemy models under `app/models` include:

- Auth: `User`, `Role`, `Group`, `Session`, `IdentityProvider`, membership tables.
- Chat: `Chat`, `Message`, `ToolCall`, `Approval`.
- Audit and usage: `AuditLog`, `Usage`.
- Automations: `ScheduledTask`, `TaskRun`.
- Workbooks/playbooks/assessments: `WorkbookRun`, `PlaybookRun`, `AssessmentRun`,
  `AssessmentWaiver`, `AssessmentFindingState`.
- Notifications: `Notification`, `NotificationDelivery`, `NotificationRule`.

Local native development currently uses SQLite. Container Apps production target uses
a PostgreSQL container with an Azure Files volume.

### 6.2 JSON Registries Under `backend/.data`

Registries are used for operational configuration and user-authored artifacts that do
not require relational querying.

| Registry | Purpose |
| --- | --- |
| `llm_config.json` | Runtime provider/model/key configuration |
| `app_settings.json` | Global settings and feature toggles |
| `azure_connections.json` | Azure tenant connections, encrypted secrets, Log Analytics workspace id |
| `workloads.json` | Workload definitions, selected nodes, Autopilot metadata |
| `architectures.json` | Architecture diagrams and metadata |
| `architecture_memory.json` | Structured Architecture Memory sections |
| `architecture_memory_revisions.json` | Memory revision history |
| `assessments_custom_checks.json` | Custom assessment controls |
| `workbooks.json` | Reusable workbook definitions |
| `playbooks.json` | Chained workbook definitions |
| `automations_agents.json` | Custom agent definitions |
| `automations_connectors.json` | Connector configuration |
| `monitor_dashboards.json` | Monitor 2.0 dashboard/widget definitions and revisions |
| `monitor_ping_history.json` | Rolling web/TCP ping samples |
| `policies_baseline.json` | Policy baselines |
| `secret.key` | Local Fernet key for encrypted registry fields |

## 7. Authentication and Authorization

### 7.1 Local Auth

- `DEV_AUTH=true` can inject a development principal.
- Local password auth is supported; the initial admin user is seeded for development.
- Sessions use opaque cookies and server-side session rows.

### 7.2 SSO

The auth package supports:

- OIDC Authorization Code + PKCE using provider discovery and JWT validation.
- SAML 2.0 SP-initiated login with signed/encrypted provider settings.
- Per-tenant identity provider configuration.

### 7.3 RBAC

Role/group-based access control lives in `app/auth/permissions.py` and
`app/core/security.py`. Admin-only sections use `require_admin`; feature endpoints use
permission-aware dependencies where implemented. Tenant scoping is enforced through
the `Principal` object and tenant filters.

## 8. Azure Connections and Governance

Azure connections are managed through `/admin/connections` and stored in
`azure_connections.json`.

Supported auth methods:

- `service_principal`
- `service_principal_cert`
- `azure_cli`
- `default_chain`
- `az_cli_token`

Important fields:

- `display_name`, `tenant_id`, `default_subscription`
- `log_analytics_workspace_id`
- `read_only`, `auto_execute_writes`, `disabled`, `is_default`
- encrypted credential fields such as client secret, certificate PEM, access token
- health status fields (`status`, `status_detail`, `last_tested`)

Read operations execute directly when enabled. Write or destructive operations are
classified server-side and routed through approval gates where applicable.

## 9. Major Product Areas

### 9.1 Chat and Deep Investigation

The chat system supports:

- Multiple isolated chats.
- SSE token streaming and live tool-step timeline.
- Incremental assistant-message persistence so history survives disconnects.
- Per-chat stream state that survives navigation.
- Scope clarification for subscription-sensitive prompts.
- Starter suggestions and generated follow-up suggestions.
- Deep Investigation and War Room flows with hypotheses, evidence, confidence, and
  memory-aware context injection.

### 9.2 Workloads

Workloads are named collections of Azure scopes/resources. A workload can include
management groups, subscriptions, resource groups, resources, and exclusions. Discovery
uses Azure Resource Graph and cached workload metadata.

Workloads feed Inventory, Assessments, Architectures, Architecture Memory, Deep
Investigation, and Monitor dashboard generation.

### 9.3 Inventory and Cost

Inventory provides:

- Resource grid and facets.
- Cost-related fields and freshness signals.
- Resource type/resource group/subscription/location filters.
- Location tab with real map tiles, multi-region selection, zoom/pan, responsive layout.
- AI natural-language inventory search.
- Staleness indicators for inventory and cost refreshes.

### 9.4 Assessments

Assessments implement Well-Architected-style checks across pillars such as security,
reliability, cost, operations, and performance.

Capabilities include:

- Assessment catalog and custom checks.
- SSE assessment runs.
- Findings by severity and pillar.
- Waivers and finding lifecycle state.
- Assessment scheduling through the central scheduler.
- Ticket creation via connectors.
- AI-designed or enhanced controls where supported.

### 9.5 Policy

Policy modules collect Azure Policy inventory and compliance snapshots, maintain
baselines, and provide deterministic and AI-assisted policy advisors.

### 9.6 Architectures

Architectures are stored diagrams with nodes, edges, groups, canvas metadata, activity,
collections, and revisions. The UI uses a visual canvas and can rebuild or reverse-engineer
an architecture from a workload.

Architecture-to-workload linking is supported. An architecture may be independent or
linked to a workload.

### 9.7 Architecture Memory

Architecture Memory is a structured knowledge base associated with architecture and
workload context. Sections include overview, pattern, expected flow, security model,
resiliency targets, observability notes, known gaps, known issues, diagnostic hints,
critical thresholds, ownership, and related context.

Memory features:

- Standalone memory screen and embedded architecture integration.
- Multi-section editor with markdown preview.
- AI generation from workload resources, architecture graph, assessments, and inventory hygiene.
- Versioning/revisions.
- Used by Deep Investigation and Monitor AI dashboard generation.

### 9.8 Workbooks and Playbooks

Workbooks are saved, reusable Azure operations:

- Runtimes: `az`, `kql` (Resource Graph), and PowerShell.
- Parameters with `{{param}}` interpolation.
- Test/preview run and persisted run history.
- AI post-processing (`summary`, `severity`, `extract`, `diff`).
- Optional tile and alert configuration.
- AI workbook designer and enhancement flows.

Playbooks chain workbook steps into repeatable workflows and support import/export.

### 9.9 Automations

Automations include:

- Custom agents with instructions, tools, model/provider, and run modes.
- Scheduled tasks with cron-like schedules and target configurations.
- In-process scheduler with bounded concurrency.
- Manual task runs and run history.
- Connector tool access and portability helpers.

### 9.10 Connectors and Notifications

Connectors provide external integrations such as Teams, Outlook, Jira, Grafana, Slack,
webhooks, ServiceNow, PagerDuty, email, Splunk, Cortex XSOAR, S3, SQS, and Azure
Service Bus.

Notifications support in-app delivery, delivery tracking, rules, and connector-backed
external delivery where configured.

### 9.11 Monitor 2.0

Monitor is a customizable observability dashboard system.

Dashboard model:

- Dashboards contain data-bound widgets on a 12-column `react-grid-layout` grid.
- Legacy builtin tiles are automatically surfaced as `type: "builtin"` widgets.
- Dashboards support params, workload links, revisions, default selection, and AI design metadata.

Widget types:

- `stat`, `chart`, `table`, `list`, `gauge`, `map`, `markdown`, `clock`, `availability`, `builtin`.

Data sources:

- `app_telemetry`
- `resource_graph`
- `log_analytics`
- `azure_metrics`
- `web_ping`
- `tcp_ping`
- `workbook_ref`
- `static`
- `none`

Backend data engine:

- Normalized `TableResult` shape for all data sources.
- Short-TTL result cache and bounded Azure CLI concurrency.
- App telemetry overview cache with single-flight burst protection.
- Web/TCP ping sampler with rolling JSON history.
- Log Analytics and Azure Monitor metrics command execution through validated `az` argv.

AI dashboard generation:

- AI can build one widget from natural language.
- AI can build a full dashboard from a workload.
- Dashboard style/archetype choices: full-stack observability, SRE live operations,
  incident commander, security and identity, cost and capacity, executive overview.
- Generation context includes workload resources, Architecture Memory, topology,
  resource-type SRE playbooks, observability coverage, latest assessment findings,
  and recent incident/chat hints.
- Generation pipeline includes a design brief pass, widget suggestions with operational
  questions and confidence, dry-run/repair, telemetry-gap widgets, and a Dashboard Critic pass.
- `ai_design` metadata persists archetype, design brief, critic output, dry-run results,
  memory use, and context digest.

## 10. Security Model

Key security controls:

- Tenant-scoped principal on every request.
- Admin-only endpoints for sensitive settings, providers, connections, and Monitor authoring.
- Encrypted Azure connection secrets at rest.
- Tool classification into read/write/destructive categories.
- Approval gates for write actions.
- Command runner rejects shell metacharacters and only executes allowlisted binaries.
- Built-in web/network tools use SSRF protection: private, loopback, link-local,
  metadata, reserved, and denylisted targets are blocked.
- Network utility tools are controlled by an admin kill switch and optional allow/deny lists.
- Prompt-injection defense: tool output is treated as untrusted, and tool text cannot
  self-approve writes or expand scope.
- Audit log for tool calls, approvals, settings changes, provider tests, connections, and other admin actions.

## 11. Observability and Operations

Implemented observability includes:

- `/healthz`, `/readyz` and OpenAPI health checks.
- Monitor overview aggregation for chats, messages, tool calls, tokens, provider usage,
  automations, connectors, Azure posture, recent activity, and live turns.
- Workbook tiles and Monitor 2.0 widgets.
- Usage and cost estimates for LLM calls.
- Tool-call latency and failure summaries.
- In-app notifications and SIEM export hooks.

The current local runtime uses a single backend process. The in-process scheduler handles
automations and monitor ping sampling. A future multi-replica deployment should add a
leader lock or external queue/worker for scheduler ownership.

## 12. Local Development

### Native local run

Backend:

```pwsh
cd c:\dev\aznetagent\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir c:\dev\aznetagent\backend --host 127.0.0.1 --port 8000
```

Frontend:

```pwsh
npm --prefix c:\dev\aznetagent\frontend run dev
```

Build verification:

```pwsh
npm --prefix c:\dev\aznetagent\frontend run -s build
cd c:\dev\aznetagent\backend
.\.venv\Scripts\python.exe -m ruff check app
```

Useful health checks:

```pwsh
Invoke-WebRequest http://127.0.0.1:8000/openapi.json -UseBasicParsing
Invoke-WebRequest http://localhost:5173 -UseBasicParsing
```

### Docker Compose

`docker-compose.yml` remains the local containerized path. It mounts Azure credentials
for local Azure access and runs frontend/backend with local support containers as configured.

## 13. Container Apps Target Architecture

The hosting constraint remains: Azure Container Apps only.

Production target components:

- Frontend container app with external ingress.
- Backend container app with external ingress.
- PostgreSQL container with Azure Files volume.
- Redis container with Azure Files volume where persistent cache/session state is required.
- Optional Keycloak or delegated external OIDC depending on tenant requirements.
- Container Apps Jobs for migrations, cleanup, and future background workers.

Tradeoffs:

- Stateful containers require Azure Files volumes and operational ownership of backups.
- Multi-replica stateless services are straightforward; stateful DB/Redis remain single-owner.
- App-level rate limits and guardrails replace managed gateway/WAF features under the
  Container Apps-only constraint.

## 14. Current Limitations and Risks

- SQLite is used for native local development; PostgreSQL container remains the production target.
- In-process schedulers are sufficient for one backend replica; multi-replica production needs leader election.
- Some Azure metrics/log widgets require resource ids, metric names, and Log Analytics workspace ids to be available.
- AI-generated dashboards deliberately create gap widgets when telemetry prerequisites are missing.
- Azure CLI/Resource Graph queries are bounded but still subject to Azure API latency and throttling.
- Entra write tools require high-privilege Graph permissions and must remain approval-gated.
- JSON registries are simple and effective locally, but highly concurrent enterprise use may eventually justify moving some registries into the database.

## 15. Roadmap

Near-term priorities:

1. Harden multi-replica scheduler ownership for Container Apps production.
2. Expand Monitor data sources for App Insights, Azure alerts, action groups, diagnostic settings, and resource health.
3. Add richer dashboard sharing/export and dashboard metadata viewer for AI design rationale.
4. Add formal evaluation harness for AI-generated investigations, workbooks, assessments, and dashboards.
5. Expand approval policies, change windows, dual approval, and remediation preview diffs.
6. Improve connector-backed ticketing/ChatOps workflows for investigations and findings.
7. Add backup/restore automation for stateful Container Apps services.

## 16. Important Files

| File | Purpose |
| --- | --- |
| `backend/app/main.py` | FastAPI app setup, startup/shutdown, router registration, scheduler start |
| `backend/app/agent/orchestrator.py` | Main chat tool loop |
| `backend/app/agent/deep_investigation.py` | Deep investigation workflow |
| `backend/app/mcp/client.py` | Azure and Entra MCP integration |
| `backend/app/exec/command_runner.py` | Safe command, Resource Graph, Log Analytics, metrics execution |
| `backend/app/core/azure_connections.py` | Azure connection registry and encryption |
| `backend/app/monitor/ai_author.py` | Monitor AI widget/dashboard generation pipeline |
| `backend/app/monitor/datasources/` | Monitor data source resolvers |
| `backend/app/monitor/playbooks.py` | Monitor SRE playbooks and dashboard archetypes |
| `frontend/src/api.ts` | Typed API client |
| `frontend/src/components/ChatView.tsx` | Main frontend shell and chat UI |
| `frontend/src/components/MonitorView.tsx` | Monitor dashboard grid and toolbar |
| `frontend/src/components/monitor/widgets.tsx` | Generic Monitor widget renderers |
| `frontend/src/components/monitor/editor.tsx` | Widget editor and AI dashboard authoring UI |
