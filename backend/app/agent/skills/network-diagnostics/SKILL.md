---
id: network-diagnostics
name: Network diagnostics
description: Localize connectivity failures across DNS, routing, NSGs, firewalls, private endpoints, transport, TLS, and application response.
bundles: azure.networking, diagnostics.network, diagnostics.sandbox, azure.monitoring
---
# Network diagnostics

1. Define source, destination, protocol, port, SNI/host header, path, expected resolution, and failure time.
2. Test in layers: DNS, route, ICMP when meaningful, TCP, TLS, then HTTP/application behavior.
3. Correlate effective routes, NSGs, Azure Firewall, peering, private DNS links, private endpoints, load balancers, and application gateways.
4. Prefer a sandbox inside the workload VNet for private-path evidence; public platform probes cannot prove private reachability.
5. Treat ping loss as inconclusive where ICMP is blocked; a successful TCP/TLS probe is stronger evidence.
6. Record exact observed status, address, certificate/SNI result, and failing hop/control.
7. Recommend the smallest reversible correction and keep network mutations behind approval.
