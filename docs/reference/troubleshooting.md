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
| Azure feature is empty or half-blind | [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/) and [Azure tenants]({{ site.baseurl }}/admin/azure-tenants/) |
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

## Example: incomplete visibility is not zero access

An IAM Access Map can display a group's grant even when its membership cannot be expanded. Follow the connection-capability and access guides above to investigate the missing evidence rather than interpreting an incomplete diagram as an empty directory.

{% include screenshot.html file="identity-accessmap-unexpanded-group.png" title="Troubleshooting example — an unreadable group still holds a grant" caption="The synthetic Group-filtered map retains the Reader path to the offline subscription while warning about unreadable groups, deny assignments, and hidden eligibility. Unknown members are not counted as proof of no access. This browser fixture illustrates the symptom; it is not a failed live Graph request or a verified repair." %}
