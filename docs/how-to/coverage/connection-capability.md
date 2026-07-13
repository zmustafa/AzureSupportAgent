---
layout: default
title: Diagnose Connection Capability
parent: Coverage operations
grand_parent: How-to guides
nav_order: 5
description: Explain collection blind spots, verify ARM and Graph reachability, and preserve least privilege.
permalink: /how-to/coverage/connection-capability/
---

# Diagnose Connection Capability

## Prerequisites

- Product permission `connections.read`.
- Access to the configured connection metadata.
- Administrative access only if a connection must be corrected.

## Route

Open `/capability`. The page is a read-only matrix: rows are connections and columns are ARM, Resource Graph, Microsoft Graph/Entra, Log Analytics, Key Vault, and gated-write capabilities.

## How to explain a missing or partial feature result

1. Open `/capability` and locate the connection used by the affected feature.
2. Find **Degraded**, **Blind**, or **Disabled** cells.
3. Open the cell explanation and read the reason and suggested correction.
4. Map the blind spot to the feature: ARM/Resource Graph affects estate and coverage collection; Graph affects identities and actor resolution; Log Analytics affects KQL; Key Vault affects data-plane secret/certificate checks; gated writes affect remediation.
5. Return to the feature only after confirming the needed surface is available.

**Expected result:** The incomplete result has a concrete authentication, audience, scope, configuration, timeout, or read-only explanation.

**Verification:** Compare the feature's selected connection with the matrix row; do not assume two connections to the same tenant have equal capabilities.

## How to run live verification safely

1. Start from the static matrix, which infers capability without Azure calls.
2. Enable **Verify live** when current proof is needed.
3. Review ARM and Microsoft Graph token/reachability results.
4. Treat workspace and vault cells as best-effort inference; the page intentionally does not probe every data-plane resource.
5. After an approved credential or role correction, run verification again and refresh the affected feature.

**Expected result:** ARM and Graph cells are confirmed or downgraded based on current reachability.

**Verification:** A successful test proves token acquisition at that moment, not access to every subscription, object, workspace, table, vault, or secret.

## How to correct a blind spot without over-privileging

1. Identify the exact operation and Azure scope required by the feature.
2. Prefer an appropriate managed identity or service principal over a short-lived pasted token for durable automation.
3. Grant only the required audience, application permission, Azure role, and resource scope.
4. Preserve `read_only` when the connection is intentionally audit-only.
5. Allow propagation time, then verify capability and rerun the smallest affected scan.

**Expected result:** The required feature works without turning unrelated capability cells green.

**Verification:** Confirm both the capability test and the feature's actual scoped operation.

## Safety and rollback

- The matrix never returns credentials or token values.
- Do not broaden permissions merely to improve the matrix.
- Roll back a permission change by removing the newly granted role/consent after confirming no dependent workflow requires it.
- Pasted ARM tokens do not provide Graph access and expire.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| ARM full, Graph blind | Configure a separate Graph audience and required consent; ARM and Graph tokens are not interchangeable. |
| Static full, live failed | Check expiry, tenant, authority/audience, consent, network egress, timeout, and Azure health. |
| Key Vault full, findings absent | Check vault-by-vault data-plane RBAC; the matrix does not perform those probes. |
| Writes unavailable | Check intentional read-only state, feature-specific permission, approval policy, and Azure RBAC. |
| Connections disagree | Compare tenant, visible subscriptions, auth method, consent, resource-level access, and read-only state. |

## Related docs

- [Connection Capability reference]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [Monitoring Coverage recipes]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/)
- [Alerts Manager recipes]({{ site.baseurl }}/how-to/coverage/alerts-manager/)
