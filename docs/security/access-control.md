---
layout: default
title: Security Access Control
parent: Security
nav_order: 2
description: Apply product permissions, roles, groups, tenant scoping, OIDC/SAML, and least privilege.
permalink: /security/access-control/
---

# Access control

API endpoints check explicit capability strings. Effective permissions are the union of direct roles and group roles. Built-in roles are admin, operator, auditor, user, and noaccess; custom roles select capabilities from the live catalog.

- **admin:** all product permissions.
- **operator:** operational permissions excluding security/settings/admin-only capabilities.
- **auditor:** read-oriented oversight plus Audit Log and Monitor.
- **user:** chat and selected self-service workload/design reads.
- **noaccess:** safe default with no application capability.

OIDC authorization code with PKCE and SAML 2.0 are implemented. JIT provisioning can create users, but should assign `noaccess` until reviewed. Authentication proves identity; authorization still comes from product roles and Azure/Graph permissions.

## Least-privilege layers

1. Product permission permits an application action.
2. Connection disabled/read-only policy controls availability and writes.
3. Azure RBAC/Graph application permissions constrain external data/action.
4. Tool write classification and approval controls gate execution.
5. Destination account/token controls constrain connector behavior.

A product admin does not automatically have Azure Owner. Conversely, a powerful Azure credential can make a narrow-looking tool dangerous; scope both layers.

## Related docs

- [Administration: Access Control]({{ site.baseurl }}/admin/access-control/)
- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
