"""Private Endpoint Resolution Debugger.

Diagnoses the #1 silent killer of PaaS-over-Private-Link: DNS resolving a private-endpoint
FQDN to a PUBLIC IP because the Private DNS zone is missing, not linked to the source VNet,
overridden by custom DNS servers, or shadowed by a stale A record / hosts file.

From a chosen sandbox VM / VNet it runs the full resolution chain via vm_exec (effective DNS
→ resolver → zone candidates → record → returned IP → public/private classification),
corroborates with the Azure-side truth (PE config, Private DNS zones, VNet links, custom DNS),
names the exact misconfiguration in plain English, generates a Bicep fix, supports N-source
side-by-side comparison, and pins evidence to the War Room.

DNS sibling of app.netcheck; reuses the same vm_exec / store / activity-feed patterns. A demo
path produces dummy runs so the UI is reviewable without a live VM."""
