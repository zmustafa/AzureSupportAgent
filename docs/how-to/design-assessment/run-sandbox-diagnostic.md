---
layout: default
title: Run a bounded diagnostic on a Sandbox VM
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 10
description: Execute a read-only in-guest command safely, keep strict mode intact, and read the resulting run record.
permalink: /how-to/design-assessment/run-sandbox-diagnostic/
feature_ids: [PERMISSION:sandbox.exec]
---

# Run a bounded diagnostic on a Sandbox VM

## Prerequisites

- Product permission `sandbox.exec`.
- A registered, tested, enabled Sandbox VM. See [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/).
- The administrator setting `sandbox_tools_enabled` on.
- A specific question the command will answer, and a command that only reads.

## Route

- Open `/admin/sandboxvms` for the console, or use Chat and Deep Investigation for the `vm_exec` agent tool.

## How to run a read-only command from the console

1. Open `/admin/sandboxvms` and select the VM. Confirm it is the host you intend and that it is not disabled.
2. Confirm **Strict mode** is on before running anything. Leave it on.
3. Enter one command that reads and does not change state. Prefer an explicit, narrow command over an exploratory one.
4. Run it, then read the **exit code** first, then standard error, then standard output. A returned command is not a successful command.
5. Review the run in history to confirm what was recorded.

**Expected result:** A completed run with an exit code, captured output, and a durable run record.

**Verification:** Confirm the run record names the VM you expected and the command you meant to send. Treat a non-zero exit code as a failure even when output looks plausible.

## How to let the agent run a diagnostic

1. Confirm the agent has the sandbox tools available — this requires both `sandbox.exec` and `sandbox_tools_enabled`.
2. Ask the question in terms of what you want to learn, and let the agent choose `vm_exec`, `vm_list`, or `vm_read_file`.
3. Read the streamed tool activity and check which VM the call targeted before accepting the conclusion.
4. If the agent proposes a destructive command on a strict-mode VM, it is blocked and returned as needing approval. Review it and run it through your change process rather than disabling strict mode.

**Expected result:** A tool call whose target, command, and output are all visible in the transcript, backed by a run record.

**Verification:** Cross-check the run record against the transcript. The transcript shows intent; the run record is the authoritative account of what executed.

## How to read the run record

1. Open run history for the VM.
2. Read the fields that matter for an incident: `status` (`running`, `succeeded`, `failed`, `timeout`, or `blocked`), `exit_code`, `destructive`, `trigger` (`manual`, `chat`, or `investigation`), `triggered_by`, and `duration_ms`.
3. For an agent-initiated run, follow `chat_id` back to the conversation to establish why it ran.

**Expected result:** A complete account of what ran, under whose authority, and with what outcome.

**Verification:** Standard output is stored to 200,000 bytes and standard error to 8,000. Output that ends abruptly was truncated by the store, not by the host.

{: .important }
Agent-initiated `vm_exec` calls are recorded in the run record rather than as a separate audit-log entry. Run history plus the chat transcript together are the reconstruction; neither is sufficient alone.

## Safety and rollback

There is no undo. A command that changes the host has changed it, and deleting the VM registration afterwards neither reverses the change nor revokes the credential.

Keep strict mode on. It is the control that stops an agent-driven or prompt-injected destructive command from running unattended on a machine inside your network, and it is on by default for that reason. Keep `allow_sudo` off unless a specific procedure requires it; with it off, escalation and automatic tool installation fail closed. Consider disabling `sandbox_auto_install` where an unplanned package installation would itself be a change worth controlling.

Capture state before any mutation and plan the reversal before running it. Assume command output may contain secrets: it is stored and readable by anyone holding `sandbox.exec`, so avoid commands that print credentials and rotate anything that leaks. To stop a host being used without losing its configuration, disable the VM rather than deleting it.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Sandbox tools are not offered in chat | Check `sandbox.exec` on the active role and `sandbox_tools_enabled` in settings. |
| "Sandbox VM not found" | The registration was removed or the identifier is stale. Re-select from the list. |
| Command returned as needing approval | Strict mode plus a destructive classification. Review and route it through change control. |
| Destructive command rejected with no approval option | The chat is read-only, such as Deep Review. This is intended. |
| Host key mismatch | Stop. Verify the fingerprint out of band before trusting the host again. |
| `timeout` status | The command exceeded `sandbox_command_timeout_seconds`, default 60. It may still be running on the host. Narrow it. |
| Runs appear to queue | Four SSH sessions run concurrently across the application. |
| Missing tool, installation fails | `allow_sudo` is off or the account cannot escalate. Install through the VM's maintenance process. |
| Output ends mid-line | Storage truncation, not host behavior. Narrow the command or write to a file and read it in parts. |

## Related docs

- [Sandbox VM diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/)
- [Manage Sandbox VMs]({{ site.baseurl }}/how-to/administration/sandbox-vms/)
- [Test connectivity between two nodes]({{ site.baseurl }}/how-to/design-assessment/test-connectivity/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
