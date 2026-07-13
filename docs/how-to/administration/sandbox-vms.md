---
layout: default
title: Manage Sandbox VMs
parent: Administration tasks
grand_parent: How-to guides
nav_order: 53
description: Register, test, use, disable, and remove dedicated SSH diagnostic hosts safely.
permalink: /how-to/administration/sandbox-vms/
---

# Manage Sandbox VMs

## Prerequisites

- Product permission `sandbox.exec`.
- A dedicated reachable SSH host, port, user, and one authentication method shown by the form: password, private key, or key with passphrase.
- Approval to run diagnostics and, separately, any mutating command or `sudo` operation.

## Route

- Open `/admin/sandboxvms`.

## How to register and test a Sandbox VM

1. Select **Add VM** and enter display name, host, port, username, and the displayed SSH authentication fields.
2. Keep **Strict mode** enabled. Leave **Allow sudo** off unless the diagnostic account and procedure require it.
3. Add workload associations or a VNet label only when they are visible and useful for scoping.
4. Save. SSH passwords, keys, and passphrases are write-only; blank secret fields on edit retain the stored values.
5. Select **Test** to establish SSH, capture the host-key fingerprint, detect the OS, and probe installed tools.
6. In the console, begin with a read-only identity or version command.
7. Review exit code, standard output, standard error, and run history before attempting another command.

**Expected result:** The VM reports a successful SSH test with environment details, and a bounded diagnostic command completes with an auditable run record.

**Verification:** Confirm the detected host and tools match the intended VM. Verify exit code and output rather than treating command submission as success.

## Safety and rollback

Validate changes in a non-production scope first, and preserve a known-good configuration for rollback.

Commands run on the remote host and may be irreversible. Keep strict mode on, use least privilege, avoid secrets in commands, and capture state before mutation. Disable the VM to stop normal use without losing configuration. Application deletion does not undo remote commands or revoke SSH credentials; perform remote rollback and rotate access separately.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Unexpected result | Re-check route, permissions, and latest refresh state before retrying. |
| SSH test fails | Check DNS, route, firewall/NSG, port, username, key format, and source-network allowlisting. |
| Host key differs | Stop and verify the expected fingerprint before accepting a changed host. |
| Tool is missing | Install it through the VM's approved maintenance process; automatic installation changes the host. |
| Command fails or times out | Narrow the command and review stderr, remote permissions, strict confirmation, and configured timeout. |

## Related docs

- [How-to guides]({{ site.baseurl }}/how-to/)
- [Sandbox VM reference]({{ site.baseurl }}/admin/azure-tenants-sandbox-vms/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
