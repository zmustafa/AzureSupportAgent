---
id: vm-availability-triage
name: Virtual machine availability triage
description: Diagnose VM reachability and health by correlating resource state, platform health, recent changes, metrics, network controls, and in-VNet probes.
bundles: azure.compute, azure.monitoring, azure.networking, diagnostics.network, diagnostics.sandbox, performance
---
# Virtual machine availability triage

1. Resolve subscription, resource id, workload, region, and the time the symptom began.
2. Check power/provisioning state and Resource Health before changing the guest.
3. Correlate recent deployments, activity-log changes, platform incidents, CPU/memory/disk/network metrics, and boot diagnostics.
4. Trace NIC, subnet, NSG, route, public/private endpoint, load-balancer, and DNS dependencies.
5. When an onboarded sandbox exists, use in-VNet DNS/TCP/TLS/HTTP probes to distinguish control-plane configuration from actual data-plane reachability.
6. Prefer the Performance Profiler for broad workload saturation evidence.
7. State what was tested, what was inferred, and what remains inaccessible. Suggest reversible checks before restart/redeploy writes.
