# Concepts & Glossary

A plain-English reference for every concept and piece of vocabulary you'll meet in Azure
Support Agent. The same definitions are available in-app from the **Help (?) → Glossary**
menu and as tooltips next to the terms themselves.

---

## Core concepts

### Workload
A **Workload** is a named group of Azure resources that together make up one application or
solution — it can mix management groups, subscriptions, resource groups, and individual
resources. Workloads are the unit everything else is scoped to: assessments, architecture
diagrams, and coverage scans all run *for a workload*. Use **✨ Autopilot** to let AI
discover and propose workloads from your estate.

### Architecture (and Architecture Memory)
An **Architecture** is a living diagram of a workload — AI reverse-engineers it from your
real resources, groups it into tiers, and draws the connections. You can refine it by hand,
overlay an assessment onto it, run **drift detection** against live Azure, and save
revisions. **Architecture Memory** is the persistent, versioned knowledge captured from
those diagrams that powers dashboards and investigations.

### Assessment (Well-Architected)
An **Assessment** scores a workload against the five Azure **Well-Architected Framework**
pillars — Security, Reliability, Cost, Operational Excellence, and Performance Efficiency —
producing an overall score out of 100, prioritized findings with remediation, and mappings
to control frameworks (**CIS**, **NIST 800-53**, **ISO 27001**). Findings have a lifecycle
(open → waived/resolved) and can be turned into tickets.

### Deep Investigation ("War Room")
Switch a chat to **Deep** mode and the agent forms multiple hypotheses and dispatches
specialist sub-agents (Networking, Identity, Compute, Storage, Security, Reliability, Cost,
Monitoring) that research **in parallel** against your live Azure data, validate each
hypothesis with evidence, and converge on a root-cause conclusion with remediation. The
result — a navigable hypothesis tree — is saved with the chat.

### Proactive Support
The umbrella for the posture/forensic dashboards that surface risk *before you ask*:
Assessments, the three **Coverage** detectors, Identity, Retirement Radar, Telemetry
Intelligence, Performance Profiler, Reservations Monitor, and the Evidence Locker.

---

## The Coverage detectors

All three audit each in-scope resource against an **editable, versioned reference baseline**,
roll gaps up to a Well-Architected pillar, and export ready-to-apply remediation. A scan is
**cached** — opening a scope shows the last saved scan; click **Refresh** to run a new one
(it runs live against Azure). Each scan is saved to **history** and can be exported as a
branded **PDF** or captured into the **Evidence Locker**.

### Monitoring Coverage (AMBA)
**AMBA** = **Azure Monitor Baseline Alerts**, Microsoft's recommended set of metric alerts
per resource type. This detector audits which recommended baseline alerts are **present**,
**missing**, or **misconfigured** (wrong threshold), and generates **Bicep / Terraform** to
close the gaps. Rolls up to *Operational Excellence*.

### Telemetry Coverage
Audits each resource's **Azure Monitor diagnostic settings** against a reference of
recommended log/metric **categories**: are settings present, are the recommended categories
enabled, and do logs ship to an **admin-approved Log Analytics workspace** (vs. drifting to
an unknown destination)? Exports **Bicep** or an **Azure Policy** assignment. Rolls up to
*Operational Excellence*. (Alerts without telemetry are useless; telemetry without alerts is
silent — AMBA and Telemetry coverage are designed to pair.)

### Backup & DR Coverage
Audits each resource's **backup and disaster-recovery** posture: is backup enabled, is there
a policy with adequate retention, did the last job succeed recently, is there an offsite /
geo-redundant copy, is a **DR pair** configured and recently **drilled** (failover-tested),
is the resource encrypted and soft-delete protected. Exports **Bicep** + a runbook. Rolls up
to *Reliability*.

---

## Other Proactive Support tools

### Identity
A posture dashboard for **Entra ID** (Azure AD): expiring secrets/certificates, users
without MFA, risky sign-ins, stale guests, and app-registration hygiene — read via the
Microsoft Graph MCP server.

### RBAC / Access Review
Collapses a full Azure RBAC scanner into task-oriented tabs: effective access, privileged &
exposure, scopes, roles & principals, insights, and diagnostics — to answer "who can do what,
where, and why."

### Retirement Radar
Tracks Azure **service retirements** and **breaking changes**, mapped to the workloads,
owners, and deadlines they affect, so nothing retires out from under you.

### Telemetry Intelligence
Analyzes the *content* of your telemetry (not just whether it exists) to surface noisy
signals, gaps, and cost-saving opportunities in your Log Analytics usage.

### Performance Profiler
A resource × **AMBA-metric heatmap** that finds bottlenecks — which resources are running
hottest against their baseline metric thresholds.

### Reservations Monitor
Tracks Azure **Reserved Instances** / savings-plan coverage and upcoming expirations.

### Evidence Locker
A **write-once, hash-stamped** snapshot store for forensic investigations and audit. Capture
a point-in-time bundle (inventory, properties, recent changes, metrics, findings) scoped to a
workload; each snapshot's **SHA-256** is recorded and re-verified on read, so it's tamper-
evident. Coverage scans and investigations can be saved here as evidence.

---

## Automations & integrations

### Sub Agents
Custom, specialized agents you define with a scoped tool-set and persona (built via an
AI-guided wizard). They can be dispatched in deep investigations or run on a schedule.

### Workbooks & Playbooks
A **Workbook** is a saved `az` / Resource Graph / PowerShell operation with AI-summarized
output. A **Playbook** chains workbooks into a multi-step, conditional flow.

### Scheduled Tasks
Recurring agent workflows that run on a schedule (e.g. "every Monday, scan Backup & DR for
*Prod* and email the PDF").

### Connectors
Outbound integrations — **Teams, Slack, Jira, ServiceNow, Grafana, email** — that route
findings and notifications to where your team already works.

### Notifications
The in-app notification center; events can also fan out to connectors.

---

## Platform & safety

### MCP (Model Context Protocol)
The standard the agent uses to talk to tools. Azure Support Agent ships two MCP surfaces: the
official **Azure MCP server** (≈65 Azure tools) and a **Microsoft Graph MCP server** (≈43
Entra ID tools). Tools are classified **read** vs **write**.

### Read-only by default · Approval-gated writes · Audit
Azure access is **read-only** out of the box. Any tool that would *change* Azure is **write-
classified** and requires explicit opt-in **and** per-action approval; every action is
recorded in the **Audit Log**. AI providers are **disabled until you configure them**, so no
data goes to any LLM until you opt in.

### Connection (Azure Tenant connection)
A stored, **encrypted** credential (service principal secret/cert, or Azure CLI sign-in) that
lets the agent read a tenant. You can connect multiple tenants, each isolated, and set a
default.

### Demo data
A complete **synthetic tenant** (the "Contoso" and "Zava" sample workloads with coverage,
assessments, identity, and more) you can load to explore every feature **without connecting
Azure**. Load/remove it from **Settings → Demo Data**, or from the first-run **Welcome**
screen. Demo data never touches Azure.

---

## Security & access model

| Control | What it means |
| --- | --- |
| **Read-only by default** | The agent reads your estate; writes are opt-in. |
| **Approval-gated writes** | Every write-classified tool call needs explicit approval. |
| **Full audit log** | Every privileged action is recorded with actor, target, and time. |
| **RBAC** | Users, roles, and groups; least-privilege by default. |
| **SSO** | OIDC and SAML sign-in. |
| **Encrypted credentials** | Azure connection secrets are encrypted at rest on the Azure Files volume. |
| **Data residency** | Everything runs in *your* subscription; data never leaves your tenant. |

See [USER_GUIDE.md](USER_GUIDE.md) for how to use each feature, and the in-app **Help →
Trust & Security** page for the live posture.
