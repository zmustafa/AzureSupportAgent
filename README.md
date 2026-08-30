<div align="center">

# 🛠️ Azure Support Agent

**An AI-driven Azure operations workbench that runs in _your_ subscription.** Point it at
your tenant and AI discovers your workloads, reverse-engineers live architecture diagrams,
and runs Well-Architected assessments — then a War Room of specialist agents helps you
investigate, monitor, and remediate.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fzmustafa%2FAzureSupportAgent%2Fmain%2Fdeploy%2Fmain.json)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-azure--support--agent-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/zmustafa/azure-support-agent)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](backend/pyproject.toml)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)](frontend/package.json)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Deploy](#-deploy-to-azure-one-click) · [Install guide](https://zmustafa.github.io/AzureSupportAgent/getting-started/one-click-install/) · [User guide](https://zmustafa.github.io/AzureSupportAgent/user-guide/) · [How-to guides](https://zmustafa.github.io/AzureSupportAgent/how-to/) · [Administration](https://zmustafa.github.io/AzureSupportAgent/admin/) · [Connectors](https://zmustafa.github.io/AzureSupportAgent/connectors/) · [Docs](https://zmustafa.github.io/AzureSupportAgent/)

</div>

> 🆕 **Latest:** a ten-tab **Entra ID** identity workbench (Conditional Access simulator, PIM, app credentials, and Investigate for any principal), **IAM** access review (effective permissions, escalation paths, least-privilege, review campaigns), **Backup Manager** and **Alerts Manager** — both with approval-gated, reversible changes — and **AI Insight Packs** that watch your estate on a schedule and notify you only when something material happens.

![Architecture designer reverse-engineering live Azure resources with AI rationale](docs/assets/architecture-designer.png)

---

## Why Azure Support Agent?

Operating Azure at scale means hopping between the Portal, CLI, Resource Graph, Monitor,
Advisor, and a dozen blades just to answer one question. **Azure Support Agent puts an LLM
in the driver's seat** — it talks to your subscription through the official **Azure MCP
server** and a **Microsoft Graph (Entra ID) MCP server**, reasons over live evidence, and
turns *"why is the website throwing 5xx?"* into a ranked, validated answer — with the
diagrams, assessments, and dashboards to back it up. And it doesn't just wait to be asked:
a whole **Proactive Support** suite continuously scans your estate for coverage gaps and
looming retirements, while scheduled autonomous agents push findings to Teams, Jira,
ServiceNow, PagerDuty, your SIEM, or Azure Logic Apps before they bite.

- 🧠 **Agentic, not just a chatbot** — a War Room of specialist agents investigates in parallel against your real Azure data.
- 🛡️ **Proactive, not just reactive** — a Proactive Support hub (Assessments · Identity · Monitoring, Telemetry & Backup/DR coverage · Retirement Radar · Telemetry Intelligence · Performance Profiler · Ownership · Tag Intelligence · Change Explorer · Estate Graph) surfaces risks before you ask, and scheduled autonomous agents notify you via connectors.
- 🏠 **Runs in your tenant** — one-click deploy to Azure Container Apps; your data never leaves your subscription.
- 🔒 **Safe by default** — Azure access is **read-only**, writes are **approval-gated + audited**, and AI providers stay **disabled until you configure them**.
- 🧰 **A whole workbench** — chat, investigations, architectures, a workloads cockpit, inventory, assessments, policy, monitoring, ownership, tagging, change forensics, an estate knowledge graph, and automations.

> Built for cloud architects, SREs, platform teams, and Azure support engineers.

## Table of Contents

- [Features](#-features)
- [Screenshots](#-screenshots)
- [Deploy to Azure (one-click)](#-deploy-to-azure-one-click)
- [Documentation portal](https://zmustafa.github.io/AzureSupportAgent/)
- [Installation guide](https://zmustafa.github.io/AzureSupportAgent/getting-started/one-click-install/)
- [User guide](https://zmustafa.github.io/AzureSupportAgent/user-guide/)
- [How-to guides](https://zmustafa.github.io/AzureSupportAgent/how-to/)
- [Administration](https://zmustafa.github.io/AzureSupportAgent/admin/)
- [Connectors](https://zmustafa.github.io/AzureSupportAgent/connectors/)
- [Quick start (local)](#-quick-start-local)
- [How it works](#-how-it-works)
- [Tech stack](#-tech-stack)
- [Security & access model](#-security--access-model)
- [Documentation](#-documentation)
- [Contributing](#-contributing)
- [License](#-license)

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### 💬 Conversational operations
Multi-session chat with isolated context, live SSE streaming, a per-message reasoning +
tool-call timeline that persists across reloads, image support, and smart starter
suggestions. Cancel a running turn anytime — work continues server-side and is saved.

</td>
<td width="50%" valign="top">

### 🕵️ Deep investigations ("War Room")
Toggle deep mode to dispatch specialist agents (Networking, Identity, Compute, Storage,
Security, Reliability, Cost, Monitoring) that research in parallel, form hypotheses, and
validate them against your live Azure data — then converge on a conclusion.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📦 Workloads cockpit
Discover and group resources into workloads, then work a fleet **cockpit** with composite
**health scores**, a resource **taxonomy**, table/board views, and rich visualizations
(donut, radar, sparkline, treemap). Drill into a per-workload **command center**, or let
**Autopilot** AI-discover and propose workloads for you.

</td>
<td width="50%" valign="top">

### 🚀 Mission Control
Run **every** analysis against a workload from one cockpit — architecture, assessment,
coverage, identity and more — and read a single go/no-go posture with the highest-risk
items surfaced first.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🗺️ Architectures + Architecture Memory
AI reverse-engineers live resources into interactive diagrams with best-practice review,
network boundaries, and cost hints. Save revisions, build collections, and keep persistent
**Architecture Memory** that powers dashboards and investigations.

</td>
<td width="50%" valign="top">

### 🕸️ Estate Graph
A live, **workload-aware knowledge graph** of your tenant with cost, retirement and RBAC
overlays — pan, zoom, search, and deep-link straight into the workload, architecture or
assessment behind any node.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🆔 Entra ID
A ten-tab identity workbench: **posture** scoring, **Conditional Access** (coverage,
exposure, conflicts, break-glass, and a policy **simulator**), **privileged access** and
PIM, **app registrations** and credential expiry, sign-in **signals**, guest and access
**governance**, an identity graph, and a findings ledger. **Investigate** any user, guest,
group, service principal or managed identity — including deleted ones — with provenance on
every section, so "unreadable" never looks like "empty".

</td>
<td width="50%" valign="top">

### 🔑 IAM
Azure RBAC you can actually reason about: **effective permissions** for any principal,
an **access map**, **privilege-escalation** and **bypass** path detection,
**least-privilege** recommendations, a **what-if simulator**, snapshot **compare**,
scope and role explorers, and **access review campaigns** with reviewer routing,
delegation, and evidence.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🪪 Ownership
A federated **owner directory** scoped by tenant, subscription or workload. **Export**
owners, **import** any CSV/Excel with AI column-inference and a preview, then **apply as
Azure tags** with snapshots and **safe revert**.

</td>
<td width="50%" valign="top">

### 🏷️ Tag Intelligence
A tag **census** with coverage, casing-drift detection and a natural-language → Azure
Resource Graph console. Propose, **apply and revert** tag changes with full **revision
history**.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🛰️ Change Explorer
See **what changed, when, and who did it** across your estate — each change AI-categorized
and **risk-scored**, with plain-English insights that float the highest-risk changes to the
top for review.

</td>
<td width="50%" valign="top">

### ✅ Assessments & governance
Run Well-Architected-style assessments across Security, Reliability, Cost, Operations, and
Performance pillars — with custom controls, framework mappings (NIST, ISO, CIS), waivers,
finding lifecycle, and ticketing. Plus Policy compliance, baselines, and AI advisors.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🗄️ Backup Manager
Beyond coverage reporting: a live **backup inventory**, job health, policy and vault
management, and **DR drills**. Changes are **approval-gated**, encrypted before/after
state is retained, and every apply has a **rollback** path.

</td>
<td width="50%" valign="top">

### 🚨 Alerts Manager
A fired-alert inbox with action-group management, **overlap** and routing analysis, and
baseline gap detection. Rule and action-group edits follow the same approval-gated,
audited, **reversible** path as Backup Manager.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🗂️ Inventory
One unified, filterable resource grid across every connected tenant, with overview,
location, **cost** and optimization views, plus a change feed — the flat estate view the
Portal never quite gives you.

</td>
<td width="50%" valign="top">

### 🧾 Evidence & Case Files
An **Evidence Locker** of investigation snapshots with diffs, sharing and export, and
durable **Case Files** that keep an incident's timeline, findings and the identity it
concerns in one place — including cases opened straight from an identity investigation.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📈 Monitoring & resilience
**Monitor 2.0** customizable dashboards with AI authoring and ping history; **AMBA**
baseline-alert coverage with one-click Bicep/Terraform gap remediation; **Performance
Profiler**, **Backup/DR coverage**, **Retirement Radar**, and telemetry intelligence.

</td>
<td width="50%" valign="top">

### 🤖 Automations & workflows
Build custom sub-agents with scoped tools, schedule recurring tasks (advanced cron
recurrence builder), chain Workbooks into Playbooks, and route results through in-app
Notifications and external connectors — Teams, Slack, Jira, ServiceNow, PagerDuty, Splunk,
Sumo Logic, CrowdStrike NG-SIEM, AWS Security Hub, Grafana, Azure Logic Apps, and more.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🛡️ Proactive Support hub
One categorized landing page that unifies **every** posture and forensic dashboard, in the
same clusters the product and the docs use:

- **Daily intelligence** — AI Insight Packs
- **Design & ownership** — Architectures · Know-Me · Ownership · Estate Graph
- **Assessment & performance** — Assessments · Performance Profiler · FMEA
- **Coverage** — Monitoring (AMBA) · Alerts Manager · Telemetry · Backup & DR ·
  Backup Manager · Connection Capability
- **Estate intelligence** — Inventory · Tag Intelligence · Change Explorer
- **Governance & identity** — Azure Policy · Entra ID · IAM
- **Lifecycle & investigation** — Retirement Radar · Reservations · Quota ·
  Telemetry Intelligence · Evidence Locker · Case Files

</td>
<td width="50%" valign="top">

### 🔌 Bring your own AI
A dozen+ providers — OpenAI, Azure OpenAI, Anthropic Claude, Google Gemini, GitHub
Copilot/Models, Grok, Mistral, OpenRouter, ChatGPT (OAuth), **Claude OAuth (Pro/Max)**, Ollama, LM Studio — switchable at runtime with live model catalogs.
**Disabled until you set them up.**

</td>
</tr>
</table>

### Enterprise-ready

🔐 Read-only Azure by default · ✅ approval-gated writes · 🧾 full audit log ·
👥 RBAC (users / roles / groups) · 🔑 OIDC + SAML SSO · 🗝️ encrypted connection
credentials · 🌐 IP allowlisting · 🖥️ Sandbox VMs for private-endpoint diagnostics ·
🧩 multi-tenant Azure connections.

## 📸 Screenshots

<table>
<tr>
<td width="50%"><img src="docs/assets/workloads-fleet.png" alt="Workloads fleet cockpit"><br/><sub><b>Workloads cockpit</b> — composite health scores, resource mix &amp; trend sparklines across your fleet.</sub></td>
<td width="50%"><img src="docs/assets/workload-detail.png" alt="Workload command center"><br/><sub><b>Workload command center</b> — health, coverage, risk &amp; next-best-actions for a single workload.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/estate-graph.png" alt="Estate Graph knowledge graph"><br/><sub><b>Estate Graph</b> — a live, workload-aware knowledge graph with cost, retirement &amp; RBAC overlays.</sub></td>
<td width="50%"><img src="docs/assets/proactive-support.png" alt="Proactive Support hub"><br/><sub><b>Proactive Support</b> — every posture &amp; forensic dashboard, grouped into one hub.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/mission-control.png" alt="Mission Control"><br/><sub><b>Mission Control</b> — run every analysis against a workload from one go/no-go cockpit.</sub></td>
<td width="50%"><img src="docs/assets/tag-intelligence.png" alt="Tag Intelligence"><br/><sub><b>Tag Intelligence</b> — tag census, coverage &amp; casing drift with a natural-language console.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/change-explorer.png" alt="Change Explorer"><br/><sub><b>Change Explorer</b> — what changed, when, who did it, how risky, and what it impacts — in plain English.</sub></td>
<td width="50%"><img src="docs/assets/architecture-designer.png" alt="Architectures designer"><br/><sub><b>Architectures designer</b> — design diagrams with AI rationale &amp; best-practice review.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/deep-investigation.png" alt="Deep investigation War Room"><br/><sub><b>War Room</b> — assemble a team of specialist agents to investigate in parallel.</sub></td>
<td width="50%"><img src="docs/assets/assessment.png" alt="Well-Architected assessment"><br/><sub><b>Assessments</b> — pillar scores, controls, and framework mappings (NIST/ISO/CIS).</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/performance-profiler.png" alt="Performance Profiler heatmap"><br/><sub><b>Performance Profiler</b> — resource × AMBA-metric heatmap to find bottlenecks.</sub></td>
<td width="50%"><img src="docs/assets/monitoring-coverage.png" alt="Monitoring coverage"><br/><sub><b>Monitoring coverage</b> — AMBA baseline-alert gaps with Bicep/Terraform fixes.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/telemetry-coverage.png" alt="Telemetry coverage"><br/><sub><b>Telemetry coverage</b> — diagnostic-settings &amp; log coverage with Bicep/Policy gap fixes.</sub></td>
<td width="50%"><img src="docs/assets/monitor-dashboard.png" alt="Monitor 2.0 dashboard"><br/><sub><b>Monitor 2.0</b> — usage, token cost, provider mix, and activity at a glance.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/ai-providers.png" alt="AI provider settings"><br/><sub><b>AI providers</b> — bring your own model; each one stays disabled until configured.</sub></td>
<td width="50%"><img src="docs/assets/backup-coverage.png" alt="Backup and DR coverage"><br/><sub><b>Backup &amp; DR coverage</b> — RTO/RPO protection posture with Bicep/runbook gap fixes.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/assets/retirement-coverage.png" alt="Retirement and breaking-change radar"><br/><sub><b>Retirement radar</b> — service retirements &amp; breaking changes mapped to workloads, owners, and deadlines.</sub></td>
<td width="50%"><img src="docs/assets/entra-findings.png" alt="Entra ID findings inbox"><br/><sub><b>Entra ID</b> &mdash; identity findings ranked by severity: standing global admins, expiring credentials, ownerless apps, MFA &amp; conditional-access gaps.</sub></td>
</tr>
</table>

## 🚀 Deploy to Azure (one-click)

> **Status: tested.** Provisions a managed PostgreSQL database, Azure Files state storage,
> and the Container App running an immutable public image digest — in **your** subscription, in one
> deployment. No CLI, no manual wiring.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fzmustafa%2FAzureSupportAgent%2Fmain%2Fdeploy%2Fmain.json)

What it creates:

1. **Azure Container App** running a digest-pinned public Docker Hub image
2. **Azure Database for PostgreSQL — Flexible Server** (managed), auto-linked via `DATABASE_URL` (`?ssl=require`)
3. **Azure Files** share mounted at `/app/.data` (registries, caches, encryption key)
4. **Container Apps environment** + external HTTPS ingress on port 8000
5. **Log Analytics**, data-service diagnostics, managed identity, and HTTP health probes

> 💰 The default remains cost-conscious: one always-running replica (scaling to at most two),
> 1 vCPU / 2 GiB, Burstable `B1ms` PostgreSQL, LRS storage, and no database HA, geo-backup,
> zone redundancy, locks, or paid alert rules. Use the supplied production preset to opt into
> resilient settings after checking regional SKU support and current Azure pricing.

You supply separate **admin and PostgreSQL passwords** (the admin is forced to change theirs
on first login) and explicitly choose the database networking acknowledgement. Then
connect your Azure tenant and an LLM from **Settings** — the AI does the rest (workload
discovery, architectures, coverage scans, assessments, retirement radar, performance
profiling). Defaults to **West US 3** (validated for Container Apps + PostgreSQL B1ms).

📖 New here? Follow the **[step-by-step installation guide](https://zmustafa.github.io/AzureSupportAgent/getting-started/one-click-install/)** — from
clicking the button to onboarding your first workload.

Prefer the CLI or want full control? See the **[manual deployment guide](https://zmustafa.github.io/AzureSupportAgent/getting-started/manual-deployment/)**.

## ⚡ Quick start (local)

**Prerequisites:** Docker Desktop · Azure CLI (`az`) · an LLM key (or a local Ollama / LM Studio).

```pwsh
# 1) Sign in to the subscription you want to work with
az login
az account set --subscription "<your-subscription-id>"

# 2) Configure environment
Copy-Item .env.example .env     # set LLM_API_KEY (optional — you can also do it in the UI)

# 3) Run the whole stack
docker compose up --build
```

Open **http://localhost:5173**. The backend runs DB migrations on startup; the first Azure
MCP call starts the server bundled in the image and caches its tool catalog. The image also
includes Azure Quick Review for compliance scans; native development hides that tool when its
optional `azqr` executable is not installed.

**Health check:** [`/healthz`](http://localhost:8000/healthz) · MCP tools (admin):
`/api/admin/mcp/tools`

Full local/dev instructions (native backend, tests, type-check) live in
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

## 🧩 How it works

The whole app — FastAPI **API + the built React SPA + the in-process MCP servers** — ships
as **one container image** and runs as a **single Container App**. No separate frontend,
database, or Redis containers required.

```mermaid
flowchart LR
    U([Browser]) --> SPA[React SPA]
    SPA -->|/api| BE[FastAPI backend<br/>orchestrator · SSE streaming]
    BE --> LLM{{LLM providers<br/>OpenAI · Claude · Gemini<br/>Copilot · Ollama · …}}
    BE --> AZ[Azure MCP server · stdio]
    BE --> EID[Entra / Graph MCP server · stdio]
    BE --> TOOLS[Built-in tools<br/>DNS · HTTP · ping · traceroute]
    BE --> DB[(PostgreSQL / SQLite)]
    BE --> FILES[[Azure Files<br/>/app/.data]]
    AZ --> SUB[(Your Azure subscription)]
    EID --> GRAPH[(Microsoft Graph)]
```

For local dev nothing is deployed to Azure — the MCP server reaches your real subscription
**outbound** using your signed-in identity and existing RBAC, **read-only by default**.

## 🔧 Tech stack

| Layer | Tech |
| --- | --- |
| **Backend** | Python 3.12 · FastAPI · async SQLAlchemy 2 · Pydantic v2 · Alembic · SSE |
| **Frontend** | React 18 · TypeScript · Vite · Tailwind · TanStack Query · Recharts · XYFlow · Cytoscape · Mermaid |
| **AI** | Provider abstraction with streaming + normalized tool-calls (a dozen+ providers) |
| **Azure** | Official Azure MCP server (`@azure/mcp`) · Azure CLI / Resource Graph runner |
| **Entra ID** | Vendored Microsoft Graph (EntraID) MCP server over stdio |
| **Data** | PostgreSQL (prod) / SQLite (local) · Azure Files for state |
| **Hosting** | Azure Container Apps (single image) |

## 🔐 Security & access model

- **Read-only by default.** The Azure MCP server starts with `--read-only`; write-capable tools are classified, **approval-gated**, and **audited**.
- **AI providers off until configured.** A fresh install ships every provider disabled; a provider only becomes selectable once you add a key (or sign in / set a local base URL).
- **Identity & SSO.** Local users with RBAC (users / roles / groups), plus OIDC and SAML SSO. Forced password change on first admin login.
- **Network access.** Restrict which source addresses can reach the application at all — an in-app IP allowlist with a monitor mode that shows what *would* be blocked before you enforce it, Container Apps ingress restrictions, or no public endpoint at all.
- **Secrets.** Connection credentials are encrypted at rest and never returned to the UI. `.env`, `backend/.data/`, and keys are git-ignored.
- **Found a vulnerability?** Please follow **[SECURITY.md](SECURITY.md)** — don't open a public issue.

## 📚 Documentation

The complete documentation is published at **[zmustafa.github.io/AzureSupportAgent](https://zmustafa.github.io/AzureSupportAgent/)**. Every major application screen includes a contextual **Help** link to its owning guide.

| Public documentation | What's inside |
| --- | --- |
| [Getting started](https://zmustafa.github.io/AzureSupportAgent/getting-started/) | Installation, first-run configuration, Azure tenant setup, and deployment |
| [User guide](https://zmustafa.github.io/AzureSupportAgent/user-guide/) | Feature-by-feature product reference |
| [How-to guides](https://zmustafa.github.io/AzureSupportAgent/how-to/) | Task-oriented operational procedures and verification steps |
| [Administration](https://zmustafa.github.io/AzureSupportAgent/admin/) | Providers, tenants, access, security, reference sets, usage, audit, and backup |
| [Connectors](https://zmustafa.github.io/AzureSupportAgent/connectors/) | Setup guides for messaging, ticketing, SIEM, webhooks, queues, and storage |
| [Security](https://zmustafa.github.io/AzureSupportAgent/security/) | Security model, credentials, approval controls, and deployment posture |
| [Permissions reference](https://zmustafa.github.io/AzureSupportAgent/reference/permissions/) | Product and Azure/Graph permissions by workflow |
| [Troubleshooting](https://zmustafa.github.io/AzureSupportAgent/reference/troubleshooting/) | Common failures, diagnostics, and recovery |
| [Concepts and glossary](https://zmustafa.github.io/AzureSupportAgent/reference/glossary/) | Product vocabulary and operating concepts |
| [Technical documentation](https://zmustafa.github.io/AzureSupportAgent/technical/) | Architecture, implementation, APIs, and deployment internals |

| Repository guide | Purpose |
| --- | --- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local dev, tests, type-check, PR guidelines |
| [SECURITY.md](SECURITY.md) | Vulnerability disclosure policy |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community guidelines |

## 🤝 Contributing

Contributions are welcome! Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** and our
**[Code of Conduct](CODE_OF_CONDUCT.md)**. Good first steps: open an issue to discuss a
change, keep PRs focused, and make sure backend tests and the frontend type-check pass.

## 📄 License

[MIT](LICENSE) © 2026 Zeeshan Mustafa ([@zmustafa](https://github.com/zmustafa))

## 🙏 Acknowledgements

- [Azure MCP server](https://github.com/microsoft/mcp) — the official Azure tool surface
- EntraID MCP server (Microsoft Graph, FastMCP) — vendored under `third_party/`
- The Model Context Protocol community

<div align="center"><sub>If this project helps you, consider giving it a ⭐ — it helps others find it.</sub></div>

