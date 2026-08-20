---
layout: default
title: Network and DNS Diagnostics
parent: Design & Ownership
grand_parent: User guide
nav_order: 7
description: Prove reachability and name resolution from inside the network, with Azure control-plane evidence beside the live result.
permalink: /user-guide/design-ownership/network-dns-diagnostics/
feature_ids: [PERMISSION:netdiag.run]
---

# Network and DNS Diagnostics

**Product permission:** `netdiag.run` for every endpoint of both tools.

## Purpose

Most of this product reads Azure's control plane. These two tools do the opposite: they run a real probe from a host **inside** the network and then place Azure's own configuration next to the result. That pairing is the point. A control-plane read can tell you a rule exists; only a probe can tell you the packet arrived.

- **Test connectivity** answers "can this source actually reach that host on that port, and if not, which rule stopped it".
- **Debug resolution** answers "what does this name resolve to from in here, and is that the private address it should be".

Both are read-only. Neither changes Azure.

## Where they run from

**App routes:** `/architectures/:id`, and `/inventory` for the DNS tool.

| Tool | Launch point | Label |
| --- | --- | --- |
| Test connectivity | Architecture canvas — right-click a node, or select a node and use the inspector panel | **🔌 Test connectivity** |
| Debug resolution | Architecture canvas — right-click a node | **🧭 Debug resolution** |
| Debug resolution | Inventory grid, on private-endpoint-eligible resources only | **🧭 Debug resolution** |

Launching from a node pre-fills the target from that node's `fqdn`, `private_ip`, or `ip` metadata. In Inventory the button appears only for private endpoints and for the resource types that commonly sit behind one — Storage, SQL, PostgreSQL, MySQL, Key Vault, Cosmos DB, Container Registry, Service Bus, App Service, and Cache.

## Prerequisites

Both tools execute their probes over SSH from an onboarded **Sandbox VM**, because that is the only host the application has inside the customer network.

- At least one Sandbox VM registered and reachable. See [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/).
- Sandbox tooling enabled by an administrator. With it disabled, probes refuse to run.
- For the Azure-side evidence panels, an Azure connection that can read networking configuration in the target scope.

A source hostname or IP can be typed instead of picking a VM, but that only labels the run. Live probing still requires an SSH-reachable sandbox; entering a source with no VM behind it returns an explicit error rather than a silent empty result.

## Test connectivity

### Inputs

Source VM, target FQDN or private IP, port (default `443`), and protocol — `tcp`, `tls`, `http`, or `https`. HTTP path and TLS SNI appear when the chosen protocol uses them.

### What it runs

A chain of steps, each recording its command, status, evidence line, raw output, and duration: **DNS → ICMP → TCP → TLS → HTTP**. Which steps run depends on the protocol.

The verdict summarizes the chain:

| Verdict | Meaning |
| --- | --- |
| `reachable` | Every step succeeded. |
| `degraded` | An HTTP step failed, or any step returned a warning. |
| `blocked` | The TCP step failed, or a DNS or TCP step failed outright. |

ICMP is reported but never decides the verdict. Blocking ping while allowing TCP is normal, so a failed ICMP step is a warning, not a conclusion.

### The evidence panel

Alongside the probe, the tool reads effective routes, NSG rules, and VNet peerings for the source NIC. When a probe is blocked and a deny rule matches the destination port, that rule is surfaced as `matched_deny` — the specific NSG rule, its priority, direction and access. This is the part that turns "it failed" into "this rule failed it".

If those control-plane reads cannot be performed, the panel reports `available: false` with the reason. The probe result still stands on its own; the evidence is a supplement, never a substitute.

### Intent mismatch

If the architecture's Memory records an expected flow that names this target or port, and the probe comes back blocked, the run is flagged with an `expected_reachable_but_blocked` mismatch. This is a text match against recorded intent, so it finds contradictions it can see and stays silent about the ones it cannot. Absence of a mismatch is not evidence that the flow is correct.

## Debug resolution

### Inputs

Target FQDN, one or more source VMs, and an optional source VNet resource ID. Selecting several sources resolves the same name from each of them side by side, which is how split-horizon and per-VNet resolver problems become visible.

### What it runs

Per source: effective DNS servers, the resolver in use, the resolution itself, CNAME chain, hosts-file overrides, classification, and a gate check. Each source returns its resolved IP, any custom DNS servers detected, and a classification of `private`, `public`, or `nxdomain`.

The run then names a root cause:

| `misconfig_kind` | Meaning |
| --- | --- |
| *(empty)* | Resolution reached the expected private address. |
| `missing_zone` | The expected `privatelink.*` zone does not exist. |
| `missing_link` | The zone exists but is not linked to the source VNet. |
| `custom_dns_override` | A custom DNS server is answering ahead of Azure-provided resolution. |
| `public_unknown` | The name resolved publicly and the private path could not be confirmed. |
| `no_resolution` | The name did not resolve. |

### Zone facts

Where an Azure connection allows it, the tool reads the truth from Azure: which `privatelink` zone the FQDN should use, whether that zone exists, whether it is linked to the source VNet, and the IP of its A record. Comparing that against what the VM actually resolved is what distinguishes "the zone is wrong" from "this VM is asking the wrong resolver".

### Generated Bicep

For a run with an identified misconfiguration, **Generate IaC** produces Bicep for the corrective private DNS configuration. It is generated text for review, and the application does not deploy it. Treat it as a proposal, apply it through your normal change process, and re-run the resolution afterwards to confirm.

## History and comparison

Both tools store their runs and both compare a new run against the previous one for the same subject.

- Connectivity runs are keyed by architecture, source, target, and port. The last **20** runs per key are retained; older ones are pruned automatically.
- DNS runs are stored per architecture and returned most-recent-first.

The diff reports which steps changed status and whether the overall verdict moved. That comparison is what makes these tools useful during a change window: run before, run after, and read the delta rather than re-reading two full transcripts.

## Preserving a result

| Action | Result |
| --- | --- |
| **Export report** | A self-contained Markdown report of the run — path, verdict, per-step results, matched deny rule, and any intent mismatch. |
| **Pin** | Records the run on the architecture's activity feed as evidence, optionally routed to the War Room. |

Both tools also offer a demo seed that generates a healthy and an unhealthy run so the workflow can be reviewed without a live sandbox VM. Seeded runs are marked `demo` and must not be read as findings about the estate.

## Limits and safety

Nothing here writes to Azure. The residual risks are about interpretation, not damage.

- **A probe proves one path at one moment.** A `reachable` verdict describes the source, target, port and time it was run with, and generalises to nothing else.
- **The source is the sandbox VM, not the application.** Results describe what that VM can reach. A different subnet gets different answers.
- **Per-step timeouts are short** — roughly 2 seconds for DNS and ICMP, 5 for TCP, 8 for TLS and HTTP. A slow-but-working path can appear as a failure. Confirm before acting on a single timeout.
- **Raw output is truncated** to the first 4,000 characters per step.
- **The evidence panel can be unavailable** without the probe being wrong, and the probe can be right while the evidence is stale.
- Commands run against a host inside a customer network. Sandbox VM strict mode, host-key pinning, and the `sandbox.exec` boundary all still apply — see [Sandbox VM diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/).

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| "No sandbox VM is onboarded for this architecture's workload" | Register a VM under `/admin/sandboxvms` and associate it with the workload, or select a VM explicitly in the picker. |
| A source was entered but probes will not run | A typed source only labels the run. Live probing needs an SSH-reachable sandbox VM selected as well. |
| "Sandbox VM tools are disabled by the administrator" | The administrative kill switch is off. This is deliberate; do not work around it. |
| The picker warns that no VM is linked to the workload | All sandbox VMs are being offered instead. Confirm the one you pick actually sits in the relevant network before believing the result. |
| A step reports that a command needs approval | The VM is in strict mode and the required tool is absent. Install it through the VM's normal maintenance process. |
| Evidence panel shows `available: false` | Control-plane reads were not possible. Check the Azure connection's networking read access and whether command execution is enabled. |
| ICMP fails but TCP succeeds | Expected on most Azure networks. Read the TCP result. |
| DNS resolves publicly when a private endpoint exists | Read `misconfig_kind`. `missing_zone`, `missing_link`, and `custom_dns_override` each require a different fix. |
| Two sources disagree | That is the finding, not a fault. Compare their effective DNS servers. |

## Related docs

- [Test connectivity between two nodes]({{ site.baseurl }}/how-to/design-assessment/test-connectivity/)
- [Debug private DNS resolution]({{ site.baseurl }}/how-to/design-assessment/debug-dns-resolution/)
- [Sandbox VM diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/)
- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
