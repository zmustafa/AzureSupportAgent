---
layout: default
title: Governance & Identity
description: Review Azure Policy, identity security posture, PIM, app registrations, and effective RBAC access.
parent: User guide
nav_order: 6
permalink: /user-guide/governance-identity/
has_children: true
---

# Governance & Identity

These views combine Azure Resource Manager, Policy Insights, and Microsoft Graph evidence. They are analysis-first: Policy simulation does not deploy, Identity does not rotate credentials, and RBAC does not alter assignments.

| Guide | Use it to |
| --- | --- |
| [Azure Policy]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/) | Inventory definitions/assignments/exemptions, analyze compliance and effective policy, plan rollout, and assess drift. |
| [Entra ID]({{ site.baseurl }}/user-guide/governance-identity/identity/) | Start here for tenant identity posture: the nine tabs, the snapshot model, and what the score does and does not mean. |
| [IAM]({{ site.baseurl }}/user-guide/governance-identity/iam/) | Review effective Azure/Entra access, privileged and data-plane exposure, scopes, roles, and diagnostics. |

### Entra ID deep dives

| Guide | Use it to |
| --- | --- |
| [Setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/) | Grant the read-only consent tiers and read the domain coverage table behind every blind spot. |
| [Posture and score]({{ site.baseurl }}/user-guide/governance-identity/entra-posture/) | Understand the weighted pillar score, coverage, history, and the refresh-to-refresh diff. |
| [Conditional Access]({{ site.baseurl }}/user-guide/governance-identity/entra-conditional-access/) | Read the coverage matrix, conflicts, break-glass candidates, policy-as-code export, and the simulator. |
| [Privileged access]({{ site.baseurl }}/user-guide/governance-identity/entra-privileged/) | Compare standing and eligible privilege, PIM configuration health, activations, and cross-plane power. |
| [Applications and consent]({{ site.baseurl }}/user-guide/governance-identity/entra-applications/) | Assess app risk, credential expiry, ownership, granted permissions, and tenant consent posture. |
| [Risk and sign-ins]({{ site.baseurl }}/user-guide/governance-identity/entra-signals/) | Interpret MFA registration, legacy authentication, failure clusters, and Identity Protection risk. |
| [Governance]({{ site.baseurl }}/user-guide/governance-identity/entra-governance/) | Review access reviews, entitlement expiry, lifecycle workflows, and governance coverage. |
| [Guests (B2B)]({{ site.baseurl }}/user-guide/governance-identity/entra-guests/) | Review the external population as a lifecycle, roll it up per partner organisation, and see which partners no cross-tenant policy names. |
| [Blast radius]({{ site.baseurl }}/user-guide/governance-identity/entra-blast-radius/) | Trace derived escalation paths from an entry point to tenant-level power. |
| [Findings and scanners]({{ site.baseurl }}/user-guide/governance-identity/entra-findings-scanners/) | Work the inbox, run proactive scanners, and apply finding workflow state. |

### IAM deep dives

| Guide | Use it to |
| --- | --- |
| [Findings and scanners]({{ site.baseurl }}/user-guide/governance-identity/iam-findings-scanners/) | Work the access-findings inbox, read its two-level grouping and server tallies, and run the ten scanners without consuming their delta. |
| [Access paths]({{ site.baseurl }}/user-guide/governance-identity/iam-access-paths/) | Evaluate an action against a scope, trace the routes to full control, and inventory the doors that are not Azure RBAC. |
| [Change and simulation]({{ site.baseurl }}/user-guide/governance-identity/iam-change-simulation/) | Read the classified access diff and its Activity Log attribution, and model a change before making it. |
| [Reviews and PIM]({{ site.baseurl }}/user-guide/governance-identity/iam-reviews-pim/) | Run certification campaigns with evidence and rollback-carrying scripts, and read standing privilege against JIT eligibility. |
| [Insights, scopes, roles and diagnostics]({{ site.baseurl }}/user-guide/governance-identity/iam-insights-diagnostics/) | Read the thirteen pivots, inspect per-scope freshness and the directory layer, and diagnose collectors that could not read. |

Before drawing conclusions, check the selected connection, cache age, partial-collection errors, and [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/).
