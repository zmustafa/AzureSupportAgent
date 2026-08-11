---
id: access-review
name: Azure and Entra access review
description: Explain effective access, privilege, inheritance, group paths, ownership paths, PIM, deny assignments, and safe revocation candidates.
bundles: access_review, azure.identity, entra.roles, ownership
---
# Azure and Entra access review

1. Start from the cached IAM access model so direct, inherited, group-derived, ownership, PIM, control-plane, and data-plane paths are evaluated together.
2. Resolve the exact principal and scope before answering who can do what.
3. Distinguish active, eligible, and activated access; never call eligibility standing access.
4. Include deny assignments, notActions, scope inheritance, and unresolved ABAC conditions.
5. Use `why_does_principal_have_access` before proposing removal and `simulate_revoke` before a real revocation.
6. Report stale or absent scan coverage explicitly.
7. Recommend least-privilege changes with the assignment id and blast radius, but keep writes behind the approval policy.
