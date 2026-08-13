---
layout: default
title: Documentation Overview
nav_exclude: true
search_exclude: true
sitemap: false
---

# Azure Support Agent — Documentation

This folder builds the published documentation site at
[zmustafa.github.io/AzureSupportAgent](https://zmustafa.github.io/AzureSupportAgent/). Read it there;
this page exists so the `docs/` folder is navigable from the repository.

## Start here

| If you want to… | Read |
| --- | --- |
| **Deploy it** to your own Azure subscription (one click) | [One-click installation]({{ site.baseurl }}/getting-started/one-click-install/) |
| Deploy via **CLI / full control** | [Manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/) |
| **Understand the concepts** & vocabulary (AMBA, War Room, Evidence Locker…) | [Concepts and glossary]({{ site.baseurl }}/reference/glossary/) |
| **Use the product** feature-by-feature | [User guide]({{ site.baseurl }}/user-guide/) |
| Follow a numbered **procedure** | [How-to guides]({{ site.baseurl }}/how-to/) |
| Configure providers, access, and integrations | [Administration]({{ site.baseurl }}/admin/) |
| Wire up **Entra ID (SSO / Microsoft Graph)** | [Microsoft Entra setup]({{ site.baseurl }}/getting-started/entra-setup/) |
| **Understand the codebase** | [Architecture]({{ site.baseurl }}/technical/architecture/) · [Technical documentation]({{ site.baseurl }}/technical/) |
| See the deep **technical spec** | [Technical specification]({{ site.baseurl }}/technical/specification/) |

## The 5-minute mental model

Azure Support Agent is an **AI operations workbench that runs in your own tenant**. It has
four pillars:

1. **Converse** — chat with an agent that reads your live Azure estate through the official
   Azure MCP and Microsoft Graph servers, and (in *Deep* mode) dispatches a "War Room" of
   specialist agents to investigate in parallel.
2. **Map** — discover **Workloads** (groups of resources that make up an app), let AI
   reverse-engineer **Architecture** diagrams from what's actually deployed, and turn them
   into **Know-Me** support runbooks.
3. **Assess** — score workloads against the **Well-Architected Framework**, run **FMEA**
   risk analysis, and use a broad **Proactive Support** suite (Monitoring / Telemetry /
   Backup-DR coverage, Alerts Manager, Backup Manager, Entra ID, IAM, Retirement Radar,
   Performance Profiler, Tag Intelligence, Change Explorer, Quota and more) — plus scheduled
   **AI Insight Packs** and one-sweep **Mission Control** — that surfaces risk before you ask.
4. **Act** — every Azure write is **read-only by default, approval-gated, and audited**;
   findings route to Teams / Slack / Jira / ServiceNow / PagerDuty, your SIEM (Splunk, Sumo
   Logic, CrowdStrike NG-SIEM), or Azure Logic Apps via connectors and scheduled agents.

> **New to the vocabulary?** The [concepts and glossary]({{ site.baseurl }}/reference/glossary/) defines every
> term used in the UI. The same glossary is available in-app under the **Help (?) menu**.

## Enterprise & security posture

- **Read-only by default** — the agent reads your estate; writes are opt-in, **approval-gated**, and **audited**.
- **Your data stays in your subscription** — deployed to your Container App; nothing leaves your tenant.
- **AI providers are disabled until you configure them** — no traffic to any LLM until you opt in.
- **RBAC** (users / roles / groups), **OIDC + SAML SSO**, **encrypted connection credentials**, and a **full audit log**.

See the in-app **Trust & Security** page (Help → Trust & Security), the
[security overview]({{ site.baseurl }}/security/), and the
[security and access model]({{ site.baseurl }}/reference/glossary/#security-and-access-model).

## Repository layout

```
backend/    FastAPI app — API, agent orchestrator, MCP layer, coverage detectors
frontend/   React + TypeScript + Vite SPA
deploy/     Bicep + compiled ARM template for one-click Azure deploy
docs/        ← you are here
```

A deeper map of the backend modules and frontend views is in the
[architecture guide]({{ site.baseurl }}/technical/architecture/).
