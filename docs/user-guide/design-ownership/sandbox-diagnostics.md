---
layout: default
title: Sandbox VM Diagnostics
parent: Design & Ownership
grand_parent: User guide
nav_order: 8
description: Run bounded in-guest commands from a host inside the network, and understand the approval, audit, and blast-radius boundaries around them.
permalink: /user-guide/design-ownership/sandbox-diagnostics/
feature_ids: [PERMISSION:sandbox.exec, ADMIN_NAV:sandboxvms]
---

# Sandbox VM Diagnostics

**Product permission:** `sandbox.exec`, for onboarding and for every execution path.

## Purpose

A Sandbox VM is a dedicated host you register inside your own network so the application has somewhere to stand. Everything else in this product observes Azure from the outside; a Sandbox VM lets a diagnostic run from a place that can actually see private endpoints, internal DNS, and traffic paths the backend cannot reach.

That capability is genuinely powerful, which is why it is the most tightly bounded thing in the product. This page is mostly about those bounds.

## Two ways a command runs

| Path | Who initiates | Route |
| --- | --- | --- |
| Administrative console | A person, one command at a time | `/admin/sandboxvms` |
| `vm_exec` agent tool | The agent, during chat or an investigation | Chat and Deep Investigation |

Both funnel through the same SSH execution path, so the same validation, timeout, audit, and approval rules apply to each. There is no route that runs a command on a sandbox VM without passing through it.

The agent additionally has `vm_list` and `vm_read_file` for read-only inspection.

## What has to be true before anything runs

Every one of these is required. Any single one being off stops execution.

- The caller holds `sandbox.exec`.
- The administrator setting `sandbox_tools_enabled` is on. This is a master kill switch for `vm_exec`, `vm_list`, and `vm_read_file`.
- The target VM exists and is not `disabled`.
- SSH succeeds against the stored credentials.
- The presented SSH host key matches the fingerprint pinned during the first successful **Test**.

That last one is worth stating plainly: the fingerprint is captured on first trust and enforced afterwards. A changed host key is rejected rather than accepted with a warning.

## Approval and destructive commands

Commands are classified before execution. A command containing a mutating verb is treated as destructive.

| Context | Destructive command behaviour |
| --- | --- |
| VM with **strict mode** on (the default) | Blocked, returned as needing approval. It does not run. |
| VM with strict mode off | Runs. |
| Read-only chat, such as Deep Review | Rejected outright, with no approval path. |

Strict mode defaults to on deliberately. The source code states the reason directly: an agent-driven or prompt-injected destructive command must not be able to auto-run on a box inside a customer VNet. Turning strict mode off removes that protection for that VM, permanently, for every future run — including agent-initiated ones.

`allow_sudo` is a separate per-VM switch. Leaving it off means privileged commands and automatic tool installation will fail rather than escalate.

### The allowlist that does not apply here

There is an application setting named `command_allowlist`, defaulting to `az`, `azd`, and `kubectl`. It governs commands run on the **application host**, not on a sandbox VM. Sandbox execution is not allowlisted; it is permissive by default and bounded by destructive-command classification, strict mode, and the account's own permissions on the VM.

Read that as the design intent it is: the protection on a sandbox VM is the account you gave it, not a list of blessed binaries.

## Limits applied to every run

| Limit | Value |
| --- | --- |
| Command timeout | `sandbox_command_timeout_seconds`, default 60 seconds |
| Concurrent SSH sessions | 4 across the whole application |
| Stored standard output | Truncated to 200,000 bytes |
| Stored standard error | Truncated to 8,000 bytes |
| Automatic tool installation | `sandbox_auto_install`, default on |

Automatic installation modifies the host. It is convenient during triage and it is still a change to a machine in your network; disable it where that matters, and expect missing-tool failures instead.

Private keys are held in memory for the duration of a connection and are never written to the application's disk.

## What is recorded

Every execution creates a run record containing the command, status, exit code, truncated output and standard error, duration, whether it was classified destructive, what triggered it (`manual`, `chat`, or `investigation`), the owning chat where applicable, and who triggered it. Statuses are `running`, `succeeded`, `failed`, `timeout`, and `blocked`.

Administrative changes to the VM registry — upsert, delete, and test — additionally write audit-log entries.

{: .important }
Agent-initiated `vm_exec` calls are captured in the run record, not as a separate audit-log entry. When reconstructing what an agent did on a host, the run history is the authoritative source, and it must be read alongside the chat transcript to establish intent.

## Credential handling

SSH passwords, private keys, and passphrases are encrypted at rest and are write-only in the interface. Editing a VM with the secret fields left blank retains the stored values. Secrets are never returned in API responses.

Deleting a VM from the application removes the registration. It does not undo anything that was run on the host and does not revoke the credentials — rotate those separately at the source.

## Limits and safety

- **A sandbox VM is a foothold inside your network.** Scope its account to diagnostics, keep strict mode on, keep sudo off, and treat the credential as production-sensitive.
- **Command output can contain secrets.** Output is stored and visible to anyone holding `sandbox.exec`. Avoid commands that print credentials, and rotate anything that leaks.
- **Prompt injection is a live concern.** Content the agent reads during an investigation can attempt to steer a tool call. Strict mode is the control that stops it turning into a destructive command; leave it on.
- **Commands are irreversible.** There is no undo. Capture state before changing anything.
- **A timeout is not a failure of the target.** At 60 seconds, a slow command reports `timeout` while it may still be running on the host.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Sandbox tools appear absent in chat | Confirm `sandbox.exec` on the active role and that the administrator has `sandbox_tools_enabled` on. |
| "Sandbox VM not found" | The VM was deleted or the identifier is stale. Re-select it from the list. |
| Command returns as needing approval | The VM is in strict mode and the command was classified destructive. Review it, then run it through your change process rather than disabling strict mode. |
| Destructive command rejected with no approval option | The chat is read-only, such as Deep Review. This is by design. |
| Host key mismatch | Stop. Verify the expected fingerprint out of band before trusting the host again. |
| Automatic tool installation fails | `allow_sudo` is off, or the account cannot escalate. Install the tool through the VM's normal maintenance process. |
| Commands queue and appear slow | Only four SSH sessions run at once across the application. |
| Output looks cut off | Standard output is stored to 200,000 bytes and standard error to 8,000. Narrow the command. |

## Related docs

- [Run a bounded diagnostic on a Sandbox VM]({{ site.baseurl }}/how-to/design-assessment/run-sandbox-diagnostic/)
- [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/)
- [Network and DNS Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/network-dns-diagnostics/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
