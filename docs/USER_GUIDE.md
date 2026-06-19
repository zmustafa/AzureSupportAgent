# User Guide

A task-oriented tour of Azure Support Agent: what each area is for, when to use it, and how
to get value fast. New to the vocabulary? Keep [CONCEPTS.md](CONCEPTS.md) open alongside.

> **Tip:** Press <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>K</kbd> anywhere to open the **Command
> Palette** and jump to any page or action. The **Help (?)** menu in the top bar has the
> Glossary, keyboard shortcuts, Trust & Security, and links back to these docs.

---

## 0. First run

When you first sign in (after the forced password change), the **Welcome** screen offers two
paths:

1. **Explore demo data** — loads a complete synthetic tenant so you can try *every* feature
   immediately, no Azure required. (Remove it anytime from **Settings → Demo Data**.)
2. **Connect your Azure** — opens the guided setup: pick an **AI provider**, add an **Azure
   tenant connection**, and you're ready.

Either way, the **Dashboard** is your home base. Its **Setup guide** tracks what's left to
configure; the **Coverage** row and **Posture & risks** panels summarize estate health.

---

## 1. Connect an AI provider  *(admin)*

**Settings → AI Providers.** The agent needs an LLM to think, and **every provider is
disabled until you configure it** — no data leaves until you opt in.

- Bring your own: OpenAI, Azure OpenAI, Anthropic Claude, Google Gemini, GitHub
  Copilot/Models, Grok, Mistral, OpenRouter, ChatGPT (OAuth), or local **Ollama / LM Studio**.
- Authenticate by API key, OAuth sign-in, or keyless local server.
- Switch the default provider/model anytime; override per chat.

## 2. Connect your Azure tenant  *(admin)*

**Settings → Azure Tenants.** Add a **Connection** so the agent can read your resources:

- Service principal (secret or certificate) or Azure CLI sign-in.
- **Read-only by default** — opt into writes (still approval-gated) when ready.
- Auto-discover the subscriptions and management groups the connection can see; validate
  Entra permissions; set a default connection.

---

## 3. Converse — Chat & Deep Investigations

**Chat** is the front door. Ask anything about your estate ("why is the website throwing
5xx?") and the agent investigates with live data, showing its **reasoning + tool-call
timeline**. You can cancel a turn anytime — work continues server-side and is saved.

Toggle **Deep investigation** to convene the **War Room**: specialist agents form and test
hypotheses in parallel, then converge on a root cause. Scope it to a Workload to stay
focused. The hypothesis tree is saved with the chat.

## 4. Map — Workloads & Architectures

- **Azure Workloads** — group resources into applications. Use **✨ Autopilot** to let AI
  discover them. Workloads are what assessments, architectures, and coverage scans run
  against.
- **Architectures** — turn a workload into a living diagram: AI reverse-engineers it,
  you refine it, overlay an assessment, run **drift detection**, and save revisions.

## 5. Assess — Well-Architected & Policy  *(admin)*

- **Assessments** — score a workload across the five pillars, with findings mapped to
  CIS/NIST/ISO, waivers, lifecycle, ticketing, and a branded **PDF** export. Run on a
  schedule or across many workloads at once.
- **Azure Policy** — explore assignments, scan compliance, simulate a guardrail before you
  enforce it (read-only), and use the **Rollout Planner** to stage audit → deny safely.

## 6. Proactive Support — find risk before it bites  *(admin)*

Grouped under **Proactive Support** in the sidebar:

| Tool | Use it to… |
| --- | --- |
| **Monitoring Coverage (AMBA)** | Find missing/misconfigured baseline alerts; export Bicep/Terraform fixes. |
| **Telemetry Coverage** | Find resources missing diagnostic settings or drifting to unapproved workspaces; export Bicep/Policy. |
| **Backup & DR Coverage** | Audit protection/RTO/RPO posture; export Bicep + runbook fixes. |
| **Performance Profiler** | Spot bottlenecks on a resource × metric heatmap. |
| **Retirement Radar** | Track service retirements & breaking changes by workload/owner/deadline. |
| **Reservations Monitor** | Track RI / savings-plan coverage and expirations. |
| **Identity** | Entra ID posture: expiring creds, MFA gaps, risky sign-ins. |
| **RBAC** | "Who can do what, where, and why" access review. |
| **Telemetry Intelligence** | Analyze telemetry content for noise, gaps, and cost. |
| **Evidence Locker** | Capture tamper-evident, hash-stamped snapshots for audit/forensics. |

**How a coverage scan works:** open a tool, pick a workload (or subscription), and click
**Refresh now / Run first scan** — it runs live against Azure and saves to **history**. Each
scan exports to **PDF** or **Save to Evidence**. The Dashboard's **Coverage** row can export a
combined **Estate Coverage PDF** across all three detectors.

## 7. Act — Automations & integrations

**Automations** groups the ways the agent works *for* you:

- **Scheduled Tasks** — recurring workflows ("weekly Backup-DR scan → email PDF").
- **Sub Agents** — specialized agents you design with scoped tools (AI-guided wizard).
- **Workbooks → Playbooks** — saved operations chained into conditional flows.
- **Connectors** — Teams, Slack, Jira, ServiceNow, Grafana, email.
- **Notifications** — in-app center + connector fan-out.

## 8. Monitor & observe

- **Monitor 2.0** — customizable dashboards (AI-authored), usage, token cost, provider mix,
  activity, and ping history.
- **Inventory** — sortable grid, world map, cost & optimization tabs, change history; search
  your estate in natural language.

---

## 9. Administration  *(admin)*

**Settings** clusters configuration:

- **Configuration** — General, AI Providers, Azure Tenants, System Prompts, Assessment &
  Architecture scoring, the three **Reference Sets** (AMBA / Telemetry / Backup-DR) + their
  change-request inboxes, Retirement Radar reference, Sandbox VMs, Connectors, and the MCP
  tool catalogs.
- **Security & access** — Access Control (Users / Roles / Groups / Sign-in & SSO), Security
  Policy, Active Sessions.
- **Observability** — Usage, Audit Log, Backup & Restore, and **Demo Data**.

## 10. Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>K</kbd> | Open the Command Palette |
| <kbd>?</kbd> | Open the Help menu |
| <kbd>Esc</kbd> | Close any dialog / overlay |

---

### Where to go next

- Don't know a term? → [Concepts & Glossary](CONCEPTS.md)
- Deploying? → [Installation](INSTALLATION.md) · [Manual deploy](DEPLOYMENT.md)
- Building on it? → [Architecture](ARCHITECTURE.md)
