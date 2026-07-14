---
layout: default
title: First-Run Setup
parent: Getting Started
nav_order: 4
description: Secure the first account, configure AI and Azure connections, and onboard a workload.
permalink: /getting-started/first-run/
---

# First-run setup

Complete this sequence immediately after deployment. Administrative configuration requires an administrator; users without those permissions can consume only the features granted to their role.

**Application route:** `/dashboard`

## 1. Secure the bootstrap account

1. Open the deployment's HTTPS URL.
2. Sign in with the bootstrap username and temporary password.
3. Complete the forced password change. The default policy requires upper case, lower case, and a digit in addition to its minimum length.
4. Store recovery information according to your organization's credential policy.

Do not keep sharing the bootstrap account. Configure named users or SSO after the initial setup.

## 2. Configure an AI provider

**Route:** `/admin/providers`

1. Open **Settings → AI Providers**.
2. Select an API-key, OAuth, or local provider.
3. Enter only the fields required by that provider. Secrets are masked after save.
4. Save and run **Test connection** where available.
5. Select a model and set the provider/model as the default.

A provider that has not been configured remains disabled. Local Ollama or LM Studio must be reachable from the deployed container, not merely from the administrator's laptop.

## 3. Connect Azure

**Route:** `/admin/tenants`

1. Open **Settings → Azure tenants** and create a connection.
2. For the one-click deployment, choose the host managed identity. Service-principal secret/certificate and temporary CLI-token methods are alternatives.
3. Keep the connection read-only initially.
4. Grant Reader to the identity at the smallest useful scope.
5. Test the connection and confirm the expected subscriptions are visible.
6. Set the intended connection as default if more than one exists.

## 4. Verify the Dashboard

Return to `/dashboard`. The setup guide should recognize the configured provider and Azure connection. Some Dashboard cards are role-gated or cache-backed; an empty coverage or posture card does not necessarily mean a zero score. It often means that no scan has run for the selected workload.

![Azure Support Agent dashboard with guided onboarding cards]({{ site.baseurl }}/assets/proactive-support.png)

## 5. Discover the first workload

**Route:** `/workloads`

1. Open **Workloads** and start **Autopilot**.
2. Select a management group or subscription that the connection can read.
3. Run the free survey. Review estate facets, filters, and the estimated grouping cost.
4. Choose a discovery preset and adjust filters or AI-call budget.
5. Run discovery.
6. Review every proposed workload's members, evidence, reasoning, and confidence.
7. Save only the candidates that represent real application boundaries.

Autopilot discovery is advisory until candidates are saved. See [Workload discovery and Autopilot]({{ site.baseurl }}/user-guide/workloads/discovery-autopilot/).

## 6. Run a smoke test

- Open `/chat`, send a low-risk inventory question, and verify streaming completes.
- Open the saved workload and run **Analyze** if you want current health signals.
- Confirm unconfigured feature cards display unavailable/unknown rather than being interpreted as healthy.
- Review application roles under **Settings → Access Control** before adding users.

## Optional setup

- Enable Microsoft Graph-backed features with [Microsoft Entra setup]({{ site.baseurl }}/getting-started/entra-setup/).
- Configure SSO and assign newly provisioned users a deliberate role. The no-access role is the safe default for unapproved users.
- Add ticketing or notification connectors only after testing them with non-sensitive data.
- Load synthetic demo data if you want to explore without querying Azure; remove it from **Settings → Demo Data** afterward.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No models appear in Chat | Save, test, and activate a provider under `/admin/providers` |
| Local model works on a laptop but not in Azure | Use an endpoint reachable from the Container App network |
| Tenant test cannot list subscriptions | Check identity selection, Reader assignment, scope, and RBAC propagation |
| Dashboard cards are blank | Select a primary workload and run the corresponding scan; check your feature permissions |
| Workload health says **Not analyzed** | Run Analyze from workload detail; missing signals are not treated as zero |
| User sees access denied | Assign an application role containing the required capability |

## Related pages

- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
- [Workloads]({{ site.baseurl }}/user-guide/workloads/)
