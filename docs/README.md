---
layout: default
title: Documentation Overview
nav_exclude: true
---

# Azure Support Agent — Documentation

Everything you need to deploy, understand, operate, and extend Azure Support Agent.

## 🚀 Start here

| If you want to… | Read |
| --- | --- |
| **Deploy it** to your own Azure subscription (one click) | [INSTALLATION.md](INSTALLATION.md) |
| Deploy via **CLI / full control** | [DEPLOYMENT.md](DEPLOYMENT.md) |
| **Understand the concepts** & vocabulary (AMBA, War Room, Evidence Locker…) | [CONCEPTS.md](CONCEPTS.md) |
| **Use the product** feature-by-feature | [USER_GUIDE.md](USER_GUIDE.md) |
| Wire up **Entra ID (SSO / Microsoft Graph)** | [ENTRA_SETUP.md](ENTRA_SETUP.md) |
| **Understand the codebase** | [Architecture]({{ site.baseurl }}/ARCHITECTURE/) · [Technical documentation]({{ site.baseurl }}/technical/) |
| See the deep **technical spec** | [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) |

## 🧭 The 5-minute mental model

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
   Backup-DR coverage, Identity, RBAC, Retirement Radar, Performance Profiler, Tag
   Intelligence, Change Explorer, Quota and more) — plus scheduled **AI Insight Packs** and
   one-sweep **Mission Control** — that surfaces risk before you ask.
4. **Act** — every Azure write is **read-only by default, approval-gated, and audited**;
   findings route to Teams / Slack / Jira / ServiceNow / PagerDuty, your SIEM (Splunk, Sumo
   Logic, CrowdStrike NG-SIEM), or Azure Logic Apps via connectors and scheduled agents.

> **New to the vocabulary?** The [Concepts & Glossary](CONCEPTS.md) defines every term used
> in the UI. The same glossary is available in-app under the **Help (?) menu**.

## 🔒 Enterprise & security posture

- **Read-only by default** — the agent reads your estate; writes are opt-in, **approval-gated**, and **audited**.
- **Your data stays in your subscription** — deployed to your Container App; nothing leaves your tenant.
- **AI providers are disabled until you configure them** — no traffic to any LLM until you opt in.
- **RBAC** (users / roles / groups), **OIDC + SAML SSO**, **encrypted connection credentials**, and a **full audit log**.

See the in-app **Trust & Security** page (Help → Trust & Security) and
[CONCEPTS.md § Security model](CONCEPTS.md#security--access-model).

## 🗂️ Repository layout

```
backend/    FastAPI app — API, agent orchestrator, MCP layer, coverage detectors
frontend/   React + TypeScript + Vite SPA
deploy/     Bicep + compiled ARM template for one-click Azure deploy
docs/        ← you are here
```

A deeper map of the backend modules and frontend views is in [ARCHITECTURE.md](ARCHITECTURE.md).
