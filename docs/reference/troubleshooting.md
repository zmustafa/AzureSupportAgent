---
layout: default
title: Troubleshooting Index
parent: Reference
nav_order: 3
description: Route common setup, access, data freshness, automation, connector, and investigation symptoms to the right guide.
permalink: /reference/troubleshooting/
---

# Troubleshooting index

| Symptom area | Start here |
| --- | --- |
| Sign-in, forbidden, session, credential, approval, audit | [Security troubleshooting]({{ site.baseurl }}/security/troubleshooting/) |
| Azure feature is empty or half-blind | [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/) and [Azure tenants]({{ site.baseurl }}/admin/azure-tenants-sandbox-vms/) |
| Coverage score/gap differs from expectation | [Coverage]({{ site.baseurl }}/user-guide/coverage/) and [Reference sets]({{ site.baseurl }}/admin/reference-sets-change-requests/) |
| Retirement, reservation, quota, telemetry, evidence, case | [Lifecycle & Investigation]({{ site.baseurl }}/user-guide/lifecycle-investigation/) |
| Task/workbook/playbook/notification run | [Automations]({{ site.baseurl }}/user-guide/automations/) |
| External delivery/ticket/SIEM/queue | [Connectors]({{ site.baseurl }}/connectors/) |
| Provider/model failures | [AI providers]({{ site.baseurl }}/admin/ai-providers/) |
| Cannot reach a host, or a name resolves to the wrong address | [Network and DNS Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/network-dns-diagnostics/) |
| A diagnostic must run from inside the network | [Sandbox VM Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/) |
| Fleet or background run stuck, partial, or lost after restart | [Durable Work Batches]({{ site.baseurl }}/admin/durable-batches/) |
| General limits/thresholds/timeouts | [General settings]({{ site.baseurl }}/admin/general-settings/) |

Always capture scope, connection, generated time, run/request ID, exact error, and whether the data was cached. Remove secrets and personal data before sharing diagnostics.
