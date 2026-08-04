---
layout: default
title: Network Access
parent: Administration
nav_order: 6
description: Restrict which IP addresses and ranges can reach the application, with a monitor mode, a self-lockout guard and a commit-confirm safety timer.
permalink: /admin/network-access/
---

# Network Access

**Permissions:** `firewall.read` to view, `firewall.manage` to change

**App route:** `/admin/firewall`

## Purpose

By default the application is reachable from anywhere on the internet, which means anyone can
load the sign-in page and attempt credentials. Network Access restricts **which source addresses
may reach the application at all**, so an unknown caller never gets as far as authentication.

This is distinct from the per-IP rate limiting on
[Security Policy]({{ site.baseurl }}/admin/security-policy-sessions/), which is *reactive* — it
responds after failed sign-ins. Network Access stops the attempt from arriving.

## The two layers

Restricting access can be done in two places, and they are not alternatives.

| Layer | Enforced by | Effect |
|---|---|---|
| **Container Apps ingress** | Azure, before the container | A refused caller never completes a TLS handshake or reaches the application. This is the strongest control. |
| **This screen** | The application itself | Manageable without redeploying, with an audit trail and a monitor mode. The request still reaches the container before it is refused. |

This screen cannot see or change ingress-level restrictions. If you configure both, remember that
"Off" here does not mean the application is unrestricted at the edge.

To configure the ingress layer, set `allowedClientIpRanges` at deployment time, or afterwards:

```bash
az containerapp ingress access-restriction set \
  --name <app> --resource-group <rg> \
  --rule-name office --ip-address 203.0.113.0/24 --action Allow
```

## Modes

| Mode | Behaviour |
|---|---|
| **Off** | No evaluation. Anyone can reach the application. This is the default. |
| **Monitor** | Records what *would* be blocked, but blocks nothing. |
| **Enforce** | Sources outside the allowed list receive a bare `403`, including on the sign-in page. |

**Start in Monitor.** It costs nothing, and the Recent blocks list tells you what your rules
would actually do before they can do it. Going straight to Enforce on a guessed range is the
usual way people lose access to their own deployment.

## Allowed sources

Each rule is a single IP address or a CIDR range, IPv4 or IPv6, with a required label. The
**Scope** column translates the prefix into a plain count — `203.0.113.0/24` shows as
`256 addresses` — so an over-broad rule is visible before you save it.

- **+ Add my IP** adds the address the *server* sees you connecting from. This is the address
  that matters; it is not necessarily what your machine believes it is.
- **+ Allow** on a row in Recent blocks pre-fills a rule for that source.
- Rules can be disabled without deleting them.
- A rule permitting every address (`0.0.0.0/0` or `::/0`) is rejected in Enforce mode, because
  it would silently turn enforcement into a no-op while the screen still read "Enforcing".

## Health probes are never blocked

`/healthz`, `/readyz` and `/version` are always reachable. Blocking the platform's health probes
would take the application down far more effectively than any attacker.

Every other path is subject to the allowlist, **including the sign-in page** — keeping unknown
sources away from it is the point of the feature.

## Safeguards against locking yourself out

This is the one screen in the product that can make itself unreachable, so it carries four
guards:

1. **Monitor mode**, so rules can be validated before they take effect.
2. **Self-IP guard** — Enforce cannot be saved unless an enabled rule covers your current
   address. The address is shown on screen.
3. **Typed confirmation** — switching to Enforce requires typing `ENFORCE`.
4. **Commit-confirm timer** — enforcement is provisional for 15 minutes and reverts to Monitor
   automatically unless you press **Keep enforcing**. If a rule is wrong and you walk away, the
   application un-blocks itself.

## Recovering from a lockout

Every recovery route uses the Azure control plane, so none of them depend on being able to reach
the application. Holding Azure RBAC on the resource group is sufficient.

| Route | Notes |
|---|---|
| Connect from an allowed source | Usually the answer. |
| `az containerapp exec` then edit `/app/.data/network_access.json` | Fastest. Takes effect within seconds; no restart needed. |
| `az containerapp update --set-env-vars IP_ALLOWLIST_DISABLED=true` | Disables the feature entirely. Creates a new revision, so allow for a cold start. |
| Edit `network_access.json` on the `appdata` file share | Works with the application completely down. |

For the ingress layer, remove the rule instead:

```bash
az containerapp ingress access-restriction remove \
  --name <app> --resource-group <rg> --rule-name office
```

## Seeding at deployment

Set `allowlistSeed` (and optionally `allowlistSeedMode`) when deploying so a new environment
comes up already protected rather than open until an administrator visits this screen. The seed
applies **only on first boot** and never overwrites a policy saved later in the app.

## Recent blocks

Records are aggregated per source address, not one row per request, so a scanner cannot flood
the view. The badge distinguishes **Blocked** (actually refused) from **Would block** (Monitor
mode) — these are opposite facts and are never conflated.

Records are pruned after 30 days.

## Audit and backup

Configuration changes are written to the [Audit Log]({{ site.baseurl }}/admin/usage-audit/) as
`firewall.update`, `firewall.confirmed`, `firewall.auto_reverted` and `firewall.blocks_cleared`.

The policy is included in [Backup & Restore]({{ site.baseurl }}/admin/backup-demo/). An imported
policy that was set to Enforce is restored as **Monitor**: a backup carries the *original*
deployment's ranges, which may not include whoever is performing the restore. The rules are
preserved so they can be reviewed and enforced deliberately.

## Related docs

- [Restrict network access by IP (how-to)]({{ site.baseurl }}/how-to/administration/network-access/)
- [Security Policy and Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/)
- [Usage and Audit Log]({{ site.baseurl }}/admin/usage-audit/)
- [Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/)
