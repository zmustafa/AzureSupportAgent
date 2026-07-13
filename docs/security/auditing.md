---
layout: default
title: Auditing
parent: Security
nav_order: 5
description: Correlate application audit events, approvals, cases, evidence, Azure activity, and destination delivery.
permalink: /security/auditing/
---

# Auditing

Application Audit Log records tenant, actor, action, target, optional provider/model, metadata, and timestamp for privileged/security-relevant operations. Additional durable records include approvals, task/workbook/playbook runs, notification deliveries, connector health, case timelines, and Evidence Locker digests.

## Correlation model
Start with UTC time and actor, then correlate application object IDs with approval/tool-call IDs, Azure Activity Log correlation IDs, ticket/incident IDs, and destination delivery records. A successful application call may precede eventual external processing; a failed external call can still have a successful approval record.

Sensitive values should be redacted before audit storage, but administrators must still treat metadata as confidential. Restrict `audit.read`, control exports, and apply organizational retention/monitoring. Forwarding through connectors is not a guaranteed SIEM cursoring service unless separately implemented and verified.

## Evidence
Use Evidence Locker for a point-in-time source bundle and Case Files for chronological decisions. SHA-256 verification detects unexpected evidence changes but does not establish who originally supplied every upstream Azure record.

## Related pages

- [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/)
- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
