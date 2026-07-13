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
| [Identity]({{ site.baseurl }}/user-guide/governance-identity/identity/) | Triage identity findings, PIM/JIT exposure, and app-registration hygiene. |
| [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/) | Review effective Azure/Entra access, privileged and data-plane exposure, scopes, roles, and diagnostics. |

Before drawing conclusions, check the selected connection, cache age, partial-collection errors, and [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/).
