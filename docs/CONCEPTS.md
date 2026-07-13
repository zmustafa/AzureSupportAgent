---
layout: default
title: Concepts & Glossary
nav_exclude: true
---

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

### Know-Me
A **Know-Me** document turns an architecture's **Memory** into a support-facing reference:
an AI-drafted triage runbook with known issues, SLA thresholds, incident-response checklists
and Mermaid diagrams. You **read** it inline, **guided-fill** the remaining gaps against a
human-completion checklist, or **edit** per-section; each section can be regenerated on its
own. Docs move through *draft → in review → published* and export to Markdown / PDF.

### Assessment (Well-Architected)
An **Assessment** scores a workload against the five Azure **Well-Architected Framework**
pillars — Security, Reliability, Cost, Operational Excellence, and Performance Efficiency —
producing an overall score out of 100, prioritized findings with remediation, and mappings
to control frameworks (**CIS**, **NIST 800-53**, **ISO 27001**). Findings have a lifecycle
(open → waived/resolved) and can be turned into tickets.

### FMEA (Failure Mode and Effects Analysis)
An **FMEA** turns an architecture's Memory into scored risk tables. Each potential failure
mode gets **Severity × Occurrence × Detection** scores (each 1–10); their product is the
**Risk Priority Number (RPN)**, colour-coded by risk band so the worst risks rise to the
top. RPN is always computed server-side (never trusted from the model). Edit cells live,
regenerate a single table, track recommended actions / owners / due dates, move a doc through
*draft → in review → published*, and export to CSV or a rich **Excel** workbook.

### Deep Investigation ("War Room")
Switch a chat to **Deep** mode and the agent forms multiple hypotheses and dispatches
specialist sub-agents (Networking, Identity, Compute, Storage, Security, Reliability, Cost,
Monitoring) that research **in parallel** against your live Azure data, validate each
hypothesis with evidence, and converge on a root-cause conclusion with remediation. The
result — a navigable hypothesis tree — is saved with the chat.

### Proactive Support
The umbrella for the posture/forensic dashboards that surface risk *before you ask*. The
sidebar organizes them into groups — **Daily intelligence** (AI Insight Packs), **Design &
ownership** (Architectures, Know-Me, Ownership, Estate Graph), **Assessment & performance**
(Assessments, Performance Profiler, FMEA), **Coverage** (the three detectors + Connection
Capability), **Estate intelligence** (Inventory, Tag Intelligence, Change Explorer),
**Governance & identity** (Azure Policy, Identity, RBAC), and **Lifecycle & investigation**
(Retirement Radar, Reservations Monitor, Quota Monitor, Telemetry Intelligence, Evidence
Locker, Case Files). **Mission Control** runs the whole sweep for a workload at once.

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

### AI Insight Packs
Scheduled AI "watchers." Each **pack** gathers change and telemetry data over a time window,
reasons over it, and pings you **only when something material happens** — cutting alert
fatigue. Author one with the AI wizard (goal → guided interview → generated pack) or by hand,
run it on-demand against a tenant / subscription / workload to test, then put it on a
schedule. Each **run** produces a digest with a change table and a verdict (*nothing notable
/ notable / urgent*) plus any security flags.

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

### Quota Monitor
Tracks subscription / region **quota** usage, limits, and headroom so deployments don't fail
because a compute or networking limit was silently reached.

### Change Explorer
Analyzes **what changed** in a workload over a time window — grouped by risk, actor, and
dependency — so a regression or drift can be traced back to the change that caused it.

### Tag Intelligence
A tag census, hygiene, and coverage lens: cost allocation by tag, drift detection, and
generated **Azure Policy** to enforce a tagging standard.

### Estate Graph
A workload-aware **knowledge graph** of the whole tenant, with cost, retirement, and RBAC
overlays, for exploring how resources connect.

### Ownership
Assigns **accountable owners** and teams across subscriptions, workloads, and resources, so
every finding has someone to route to.

### Connection Capability
Shows what each Azure **connection** can actually reach — ARM, Microsoft Graph, Log
Analytics, Key Vault, and gated writes — surfacing the blind spots that would otherwise make
an answer half-blind.

### Case Files
Durable incident **case files** on a single append-only timeline: findings → investigation →
evidence → remediation → verification, surviving refresh and reassignment.

### Mission Control
Runs *every* analysis for a workload in one coordinated **mission** sweep — architecture,
assessment, performance, all three coverage detectors, FMEA, and Retirement Radar — streaming
live progress you can watch, re-run per-system, and revisit from history.

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
*Prod* and email the PDF"). An advanced recurrence builder compiles a cron expression from an
interval, weekdays, day-of-month, months and one or more times of day, with a live preview of
the next runs. The same scheduler powers **AI Insight Packs**.

### Connectors
Outbound integrations that route findings and notifications to where your team already
works: messaging (**Teams**, **Slack**), email (**Outlook**, **SMTP**), ITSM & on-call
(**Jira**, **ServiceNow**, **PagerDuty**), SIEM & security (**Splunk**, **Sumo Logic**,
**CrowdStrike NG-SIEM**, **AWS Security Hub**, **Cortex XSOAR**), dashboards (**Grafana**),
cloud & eventing (**Azure Logic Apps**, **Azure Service Bus**, **Amazon SQS/S3**), and
generic **webhooks**.

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
