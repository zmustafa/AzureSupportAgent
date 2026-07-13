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

Use no real secrets or identifiers in examples. Report product vulnerabilities through the repository's security process rather than a public issue.
