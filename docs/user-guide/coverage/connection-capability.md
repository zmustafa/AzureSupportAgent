---
layout: default
title: Connection Capability
parent: Coverage
grand_parent: User guide
nav_order: 5
description: Understand which Azure surfaces each configured connection can reach and expose investigation blind spots.
permalink: /user-guide/coverage/connection-capability/
---

# Connection Capability

**Product permission:** `connections.read`.

## Purpose

**App route:** `/capability`
Connection Capability is a read-only matrix of configured Azure connections and the data surfaces that features depend on. It helps explain why two scans over the same estate can return different results.

## Prerequisites and data sources



## Tabs and actions

### Matrix and controls

The page has one matrix rather than feature tabs. Rows are connections; columns cover capabilities such as ARM control plane, Azure Resource Graph, Microsoft Graph token acquisition, Entra directory features, Log Analytics, Key Vault data access, and gated writes. Summary cards count complete connections and blind spots. Cell detail explains the reason and suggested correction.

- **Full**: configuration indicates the capability is available; live validation may still be scope-specific.
- **Degraded**: partially configured, short-lived, unverified, or missing a recommended dependency.
- **Blind**: the connection cannot provide that surface.
- **Disabled**: the connection itself is disabled.

## Freshness and scope behavior



## Workflow overview

### Workflow

1. Open `/capability` before an estate-wide scan or when a result looks incomplete.
2. Find blind or degraded cells on the connection used by the feature.
3. Open the cell explanation and identify whether the cause is auth method, missing audience/token, absent workspace configuration, read-only state, or disabled connection.
4. Where safe, run live verification for ARM and Graph.
5. Correct the connection in the administrative connection settings—for example, use an appropriate service principal or managed identity and grant only required scope.
6. Return to the matrix, verify again, and then refresh the affected feature's data.

Do not broaden permissions merely to turn every cell green. A deliberately read-only or Azure-only connection can be correct for its purpose.

## Interpretation of results

### Capability implications

- **ARM/Resource Graph blind**: inventory, policy, coverage, and RBAC collection will be absent or partial.
- **Graph/Entra blind**: identity names, app registrations, PIM, group expansion, and actor resolution may be unavailable.
- **Log Analytics degraded/blind**: KQL-backed telemetry investigation cannot run for the intended workspace.
- **Key Vault blind**: secret/certificate expiry checks may be missing even when vault resources are visible through ARM.
- **Gated writes blind/degraded**: remediation controls remain disabled or require approval; this is often the safest expected state.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Security and limitations

- The matrix returns metadata and reasons, not stored credentials or token values.
- Live token success proves token acquisition at that moment, not access to every subscription, workspace, vault, table, or object.
- Pasted tokens expire and often cover only one audience; an ARM token is not a Graph token.
- Managed identity behavior depends on where the app runs and the identities/roles assigned there.
- A timeout is shown as degraded/failure even when Azure later recovers.
- Changes to credentials and roles may take time to propagate.

## Troubleshooting


### Static and live verification

Static mode infers capabilities immediately from non-secret connection metadata and auth method. It does not make Azure calls and is not cached.

**Verify live** attempts ARM and Microsoft Graph token/reachability checks and downgrades cells when proof fails or times out. Data-plane access to every Log Analytics workspace or Key Vault is intentionally not probed; those columns remain inference/best-effort because access is resource-specific and broad probing would be expensive and intrusive.

| Symptom | Check |
| --- | --- |
| ARM is full but Graph is blind | Configure Graph-capable application permissions/token acquisition; ARM and Graph use different audiences. |
| Live verification fails but static is full | Check credential expiry, tenant, network egress, authority/audience, consent, and Azure service health. |
| Key Vault is full but identity scan misses secrets | Verify data-plane RBAC on each vault; the matrix does not probe vault-by-vault. |
| Writes remain unavailable | Confirm the connection is intentionally writable, the user has the feature-specific permission, and approval policy allows the action. |
| Results differ by connection | Compare subscription scope, tenant, Graph consent, workspaces, vault access, and read-only flags. |

## Related pages

- [Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/)
- [Identity]({{ site.baseurl }}/user-guide/governance-identity/identity/)
- [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/)
