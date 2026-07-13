---
layout: default
title: Configure AI providers
parent: Administration tasks
grand_parent: How-to guides
nav_order: 51
description: Add, test, select, and retire supported model providers without exposing credentials.
permalink: /how-to/administration/ai-providers/
---

# Configure AI providers

## Prerequisites

- Product permission `settings.write`.
- A provider account, approved model, and either the credential or OAuth access required by the provider card.
- Network egress from the application to a hosted endpoint, or reachability to an Ollama or LM Studio endpoint.

## Route

- Open `/admin/providers`.
- Open `/admin/usage`.

## How to configure and verify an AI provider

1. Select a provider card. The live catalog is authoritative; it includes key-based, OAuth, Azure OpenAI, routing, and local OpenAI-compatible providers supported by the build.
2. Enter only fields displayed for that provider: model, credential or OAuth sign-in, and an endpoint or API version when shown.
3. Save the provider. A blank saved secret field preserves the existing write-only value; entering a value replaces it.
4. Select **Refresh models** and inspect the discovery stages. Apply **Free only** or model visibility filters only when shown.
5. Select the intended model and active provider, then save.
6. Select **Test provider** and review configuration, endpoint/DNS, connection, authentication, request, and first-token diagnostics.
7. Start a new, non-sensitive chat to confirm runtime selection.

**Expected result:** The provider is enabled, the intended model is selectable, and the staged test completes successfully.

**Verification:** Reopen `/admin/providers`, confirm the active provider and model, then check `/admin/usage` after the test chat. The credential remains masked; it is never displayed back to the browser.

## Safety and rollback

Validate changes in a non-production scope first, and preserve a known-good configuration for rollback.

Provider requests can transmit prompts and retrieved context outside the application. Confirm residency, retention, and contractual policy first. To roll back, restore the previous active provider/model, disable the new provider, or sign out its OAuth session. Rotating a secret requires entering the replacement; do not try to recover the stored value.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Unexpected result | Re-check route, permissions, and latest refresh state before retrying. |
| DNS or connection stage fails | Check the visible endpoint, container egress, proxy/firewall, and TLS trust. |
| Authentication fails | Re-enter the key or complete OAuth again; confirm account scope and expiry. |
| Model list is empty | Refresh, check account entitlement, and clear hidden or free-only filters. |
| First token times out | Try an entitled model and review provider throttling and the configured request timeout. |

## Related docs

- [How-to guides]({{ site.baseurl }}/how-to/)
- [AI Providers reference]({{ site.baseurl }}/admin/ai-providers/)
- [Usage and Audit Log]({{ site.baseurl }}/how-to/administration/usage-audit/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
