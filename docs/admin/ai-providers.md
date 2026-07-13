---
layout: default
title: AI Providers
parent: Administration
nav_order: 1
description: Configure model providers, credentials, model visibility, OAuth sessions, and connectivity diagnostics.
permalink: /admin/ai-providers/
---

# AI Providers

**Permission:** `settings.write`

## Purpose

**App route:** `/admin/providers`
The provider screen exposes the provider catalog implemented by the application, including hosted, OAuth, routing, and local OpenAI-compatible options. The exact providers and fields shown by your build are authoritative.

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Configure a provider

1. Select a provider and enter its model, credential or OAuth sign-in, and optional base URL/API version where shown.
2. For OpenRouter, use **Free only** only if free-route limitations are acceptable.
3. Save, then **Refresh models** or **Test provider**. Diagnostics cover configuration, endpoint/DNS, connection, authentication, request, and first-token phases.
4. Use **Manage visibility** to hide inappropriate models from the picker.
5. Mark unused providers disabled and sign out OAuth providers when retiring them.

Credentials are masked and not returned to the browser. Leaving a saved secret field blank preserves the existing value. Local providers such as Ollama or LM Studio still need network reachability from the application container.

## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Governance

AI is inactive until configured. Provider requests may send prompts, retrieved Azure evidence, and conversation context to that provider; review residency, retention, contractual, and model policies before enablement. Model lists and costs can change independently of the app.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| DNS/connect fails | Verify endpoint URL, container egress, proxy/firewall, and TLS. |
| Authentication fails | Rotate/re-enter the key or complete OAuth again; verify required scopes. |
| No models | Refresh, check provider account entitlements, and inspect hidden/free-only filters. |
| First token times out | Test a smaller model/request and review provider throttling. |

## Related pages

### Related docs

- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
- [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/)
