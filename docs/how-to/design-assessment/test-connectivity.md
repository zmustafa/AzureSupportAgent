---
layout: default
title: Test connectivity between two nodes
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 8
description: Prove whether a source can reach a target host and port, and identify the rule that blocked it.
permalink: /how-to/design-assessment/test-connectivity/
feature_ids: [PERMISSION:netdiag.run]
---

# Test connectivity between two nodes

## Prerequisites

- Product permission `netdiag.run`.
- A Sandbox VM onboarded and reachable in the network the traffic should originate from. See [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/).
- Sandbox tooling enabled by an administrator.
- For the deny-rule evidence panel, an Azure connection able to read effective routes, NSG rules, and peerings for the source NIC.
- The target's FQDN or private IP, and the port the application actually uses.

## Route

- Open `/architectures/:id` and select the architecture containing the two nodes.

## How to run a connectivity test

1. Right-click the **target** node on the canvas and choose **🔌 Test connectivity**, or select the node and use the same button in the inspector panel. The target field is pre-filled from the node's `fqdn`, `private_ip`, or `ip` metadata.
2. Choose the **source** Sandbox VM. Confirm it sits in the network the real client traffic comes from — a VM in a different subnet answers a different question.
3. Check the target, set the **port**, and choose the **protocol**: `tcp`, `tls`, `http`, or `https`. Supply the HTTP path or TLS SNI when those fields appear.
4. Start the test and watch the steps stream: DNS, ICMP, TCP, then TLS and HTTP where the protocol requires them.
5. Read the verdict — `reachable`, `degraded`, or `blocked` — and then read the step that produced it rather than the verdict alone.
6. If the verdict is `blocked`, open the evidence panel and look for a matched deny rule naming the NSG, rule name, priority, and direction.

**Expected result:** A verdict backed by a per-step transcript, each step showing its command, status, evidence line, and duration.

**Verification:** Confirm the source, target, and port in the result header are the ones you intended. Ignore a failed ICMP step when TCP succeeded — blocking ping while allowing TCP is normal.

## How to compare a run against the previous one

1. Run the test before the change, using the same source, target, and port.
2. Make the change through your normal process.
3. Run the test again with identical inputs. Runs are keyed by architecture, source, target, and port, so the tool compares against the prior run automatically.
4. Read the diff: which steps changed status, and whether the overall verdict moved.

**Expected result:** A step-level delta rather than two transcripts to compare by eye.

**Verification:** The last 20 runs per key are retained. If the expected baseline is missing, it was pruned or the inputs differ — check the source, target, and port match exactly.

## How to preserve the result as evidence

1. Use **Export report** to download a self-contained Markdown record of the run, including the matched deny rule and any intent mismatch.
2. Use **Pin** to record the run on the architecture's activity feed, optionally routing it to the War Room.

**Expected result:** A durable, shareable record that survives the modal closing.

**Verification:** Reopen the activity feed and confirm the pinned entry references the correct run and verdict.

## Safety and rollback

This test performs no Azure writes and creates nothing that needs rolling back. The risk is in over-reading the result.

A verdict describes exactly one source, one target, one port, and one moment. It does not generalise to other clients, other ports, or other times. Per-step timeouts are short — around 2 seconds for DNS and ICMP, 5 for TCP, 8 for TLS and HTTP — so a slow but functioning path can present as a failure; repeat before acting on a single timeout. When the evidence panel reports itself unavailable, the probe result still stands, but the deny-rule attribution does not exist for that run.

Probes execute on a host inside your network under the sandbox VM's own account, so the strict-mode and audit boundaries described in [Sandbox VM diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/) apply here too.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| "No sandbox VM is onboarded for this architecture's workload" | Register a VM at `/admin/sandboxvms` and link it to the workload, or select one explicitly. |
| A source host was entered but probes do not run | A typed source only labels the run. Select an SSH-reachable sandbox VM as well. |
| "Sandbox VM tools are disabled by the administrator" | The master kill switch is off. Do not work around it; raise it with the administrator. |
| Picker warns no VM is linked to this workload | Every sandbox VM is being offered. Verify the one you choose is in the right network before trusting the verdict. |
| A step reports that a command needs approval | The VM is in strict mode and a required tool is missing. Install it through the VM's maintenance process. |
| Evidence panel reports `available: false` | Control-plane reads failed. Check the Azure connection's networking read access. |
| Verdict is `blocked` but no deny rule is named | The block was not attributable to an NSG rule the connection can read. Check routes, peerings, and any firewall outside Azure's control plane. |
| Verdict contradicts recorded intent | The run is flagged with an intent mismatch. Treat it as a discrepancy to investigate, not as proof either side is wrong. |

## Related docs

- [Network and DNS Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/network-dns-diagnostics/)
- [Debug private DNS resolution]({{ site.baseurl }}/how-to/design-assessment/debug-dns-resolution/)
- [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/)
- [Connection Capability]({{ site.baseurl }}/user-guide/coverage/connection-capability/)
- [How-to guides]({{ site.baseurl }}/how-to/)
