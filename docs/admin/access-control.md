---
layout: default
title: Access Control
parent: Administration
nav_order: 4
description: Manage users, custom roles, groups, and OIDC or SAML sign-in providers.
permalink: /admin/access-control/
feature_ids: [ACCESS_NAV:users, ACCESS_NAV:roles, ACCESS_NAV:groups, ACCESS_NAV:identity]
---

# Access Control

**Permission:** `users.manage`

## Purpose

**App routes:** `/admin/access`, `/admin/users`, `/admin/roles`, `/admin/groups`, `/admin/identity`

## Prerequisites and data sources



## Tabs and actions

### Users

Create a local or SSO-only user with username, email, display name, optional initial password, direct roles, groups, and first-sign-in password-change requirement. Edit status/assignments, reset a local password, sign out all sessions, or delete. The list shows effective roles (direct plus inherited), auth source, status/lock, and last login.

New SSO users should remain in the safe `noaccess` role until reviewed. Password reset signs out active sessions.

### Roles

Built-in system roles cannot be edited or deleted. Current built-ins are **admin**, **operator**, **auditor**, **user**, and **noaccess**. Create custom roles by selecting exact capabilities from the grouped catalog. Avoid wildcard assumptions: API enforcement uses the displayed capability strings.

### Groups

Local groups have name, description, and roles granted to members. Users receive the union of direct and group roles. Group assignment is useful for governance, but periodic review is required because a single broad group role can expand every member's access.

### Sign-in & SSO

Create OIDC or SAML 2.0 providers. OIDC fields include issuer, optional discovery URL, client ID/secret, scopes, group claim, and optional account-selection prompt. SAML fields include entity ID, SSO URL, signing certificate, and optional email/name/group attributes. Configure display/button label and enabled state.

Use the exact generated redirect URI, ACS URL, and metadata URL shown after creation. **Test connection** validates discovery/JWKS or certificate configuration but cannot replace a real user round trip. OIDC uses authorization code with PKCE; SAML assertions are signature-validated.

## Freshness and scope behavior



## Workflow overview



## Interpretation of results



## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

### Related docs

- [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/)
- [Access control security model]({{ site.baseurl }}/security/access-control/)
- [Permissions reference]({{ site.baseurl }}/reference/permissions/)
