---
layout: default
title: Security Troubleshooting
parent: Security
nav_order: 6
description: Diagnose sign-in, authorization, session, encryption, approval, egress, and audit issues safely.
permalink: /security/troubleshooting/
---

# Security troubleshooting

| Symptom | Safe checks |
| --- | --- |
| OIDC redirect/sign-in fails | Compare generated redirect URI exactly; verify issuer/discovery, client ID/secret, PKCE-compatible web registration, clocks, and TLS. |
| SAML assertion rejected | Verify ACS/entity ID, signing certificate/current key, signed assertion, attributes, and clock skew. |
| User signs in but sees no app | JIT may have assigned `noaccess`; review direct/group roles. |
| API returns forbidden | Identify the exact product capability, then Azure/Graph permission and connection read-only state. |
| Session expires early | Compare idle and absolute policy; inspect last-seen and server clocks. |
| Stored secret no longer works | Rotate/re-enter it; check whether encryption key or deployment volume changed. |
| Write remains pending | An authorized approver must decide it; inspect request scope and reason. |
| Approved write did not apply | Read execution error and verify external state/credential/RBAC; approval is not success. |
| Connector URL rejected | HTTPS, allowed host, DNS resolution, and SSRF policy may block it. Do not weaken checks for private/untrusted targets. |
| Audit record missing | Confirm tenant/time/action and permission; correlate feature-specific run/case records and server logs. |

Preserve current logs and evidence before changing policy. Do not paste credentials into support channels. If access recovery would require bypassing authentication or audit controls, use the documented deployment recovery process and record the change externally.

## Related pages

- [Troubleshooting index]({{ site.baseurl }}/reference/troubleshooting/)
- [Access Control]({{ site.baseurl }}/admin/access-control/)
