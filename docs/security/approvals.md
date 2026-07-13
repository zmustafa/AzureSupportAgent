---
layout: default
title: Approvals
parent: Security
nav_order: 3
description: Understand write classification, approval decisions, connection policy, and verification.
permalink: /security/approvals/
---

# Approvals

Mutating tool calls are classified `write` and normally enter `awaiting_approval`. An Approval record links the request to its tool call, requester, decision, approver, reason, and timestamps. Approving authorizes execution; it does not guarantee the external system accepts it.

## Review checklist
1. Confirm requester, tenant/connection, target scope, and exact operation.
2. Read generated command/payload and before/after preview.
3. Check least privilege, blast radius, idempotency, rollback, maintenance window, and cost.
4. Reject ambiguous or secret-bearing requests with a reason.
5. After approval, inspect execution result and independently verify Azure/destination state.
6. Attach evidence and outcome to the case/change record.

`auto_execute_writes` can bypass the wait and should remain disabled unless an equivalent approved control exists. A connection marked read-only blocks destructive execution even when a user can approve. Coverage change requests often update references or create proposed IaC; approval does not mean generated infrastructure was deployed.

## Related pages

- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Alerts Manager]({{ site.baseurl }}/user-guide/coverage/alerts-manager/)
