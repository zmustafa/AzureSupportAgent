"""Private Network Reachability Analyzer.

Turns the Architecture Designer into a live network test bench: from a sandbox VM inside
(or peered to) a workload's VNet, runs real probes via vm_exec (DNS → ICMP → TCP → TLS →
HTTP), corroborates with Azure control-plane evidence (effective routes / NSG rules /
peering), compares observed reachability against the architecture Memory's expected_flow,
overlays the result on the canvas path, persists runs for re-run/diff, and pins evidence
to the activity feed / War Room.

Probes are REAL (vm_exec over SSH), never synthetic. A demo path produces dummy runs so
the UI is reviewable without a live VM."""
