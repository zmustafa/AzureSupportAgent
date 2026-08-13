---
layout: default
title: Security
nav_order: 22
description: Understand data flow, least privilege, approvals, credentials, audit controls, and secure operations.
permalink: /security/
has_children: true
---

# Security

Azure Support Agent runs in the deployed environment and is designed around read-only defaults, explicit write approvals, tenant scoping, RBAC/SSO, encrypted credentials, and audit records. Security still depends on deployment configuration, provider contracts, Azure/Graph grants, and administrator choices.

## Guides

- [Data flow]({{ site.baseurl }}/security/data-flow/)
- [Access control]({{ site.baseurl }}/security/access-control/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Security troubleshooting]({{ site.baseurl }}/security/troubleshooting/)

## Procedures

| Task | Recipe |
| --- | --- |
| Create users, roles, and groups, and connect an SSO provider | [Manage users, roles, groups, and SSO]({{ site.baseurl }}/how-to/administration/access-control/) |
| Set password, lockout, and session policy, and revoke a session | [Set policy and revoke sessions]({{ site.baseurl }}/how-to/administration/security-sessions/) |
| Restrict which IP addresses can reach the application | [Restrict network access by IP]({{ site.baseurl }}/how-to/administration/network-access/) |
| Store and rotate Azure and provider credentials | [Manage Azure tenants]({{ site.baseurl }}/how-to/administration/azure-tenants/) |
| Review privileged actions and export them to a SIEM | [Review usage and audit history]({{ site.baseurl }}/how-to/administration/usage-audit/) |

See the [permissions reference]({{ site.baseurl }}/reference/permissions/) for the capability keys behind every gate.

Use no real secrets or identifiers in examples. Report product vulnerabilities through the repository's security process rather than a public issue.
