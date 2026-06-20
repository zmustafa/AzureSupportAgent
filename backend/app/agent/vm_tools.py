"""Sandbox-VM tools exposed to the agent.

The primary surface is ``vm_exec``: the LLM is told (in the system prompt) the box's OS
and installed toolkit, then composes its own commands and runs them via this tool. We do
NOT hand-code a fixed menu of per-utility wrappers — the LLM's own command authoring IS
the surface. ``vm_list`` lets it see which sandboxes/OS/tools are available; ``vm_read_file``
is a read-only convenience.

Tools plug into the existing :class:`ConnectorToolset` so the orchestrator's tool-call
loop dispatches them uniformly. Each execution is persisted as a ``VmRun`` row.
"""
from __future__ import annotations

import logging
import re
import shlex
from datetime import datetime, timezone
from typing import Any

from app.connectors.base import ConnectorTool, ConnectorToolset, err, ok
from app.core.app_settings import load_settings
from app.exec.ssh_runner import is_destructive, run_ssh_capture

logger = logging.getLogger("app.agent.vm_tools")

_MAX_TOOL_OUTPUT = 24_000  # chars fed back to the model per call

# Some commands ship in a differently-named package. Map command -> package per family.
# (Debian/apt is where names diverge most; RPM/apk usually match the command.)
_PKG_NAMES: dict[str, dict[str, str]] = {
    "apt-get": {
        "dig": "dnsutils", "nslookup": "dnsutils", "host": "dnsutils",
        "nc": "netcat-openbsd", "ncat": "ncat", "mtr": "mtr-tiny",
        "ss": "iproute2", "ip": "iproute2", "ifconfig": "net-tools",
        "netstat": "net-tools", "route": "net-tools", "arp": "net-tools",
        "traceroute": "traceroute", "tracepath": "iputils-tracepath",
        "ping": "iputils-ping", "tcpdump": "tcpdump", "nmap": "nmap",
        "jq": "jq", "curl": "curl", "wget": "wget", "openssl": "openssl",
        "telnet": "telnet", "whois": "whois", "socat": "socat",
    },
    "dnf": {"dig": "bind-utils", "nslookup": "bind-utils", "host": "bind-utils", "nc": "nmap-ncat"},
    "yum": {"dig": "bind-utils", "nslookup": "bind-utils", "host": "bind-utils", "nc": "nmap-ncat"},
    "apk": {"dig": "bind-tools", "nslookup": "bind-tools", "host": "bind-tools"},
    "zypper": {"dig": "bind-utils", "nslookup": "bind-utils"},
    "pacman": {"dig": "bind", "nslookup": "bind"},
}

# Non-interactive install command template per package manager.
_INSTALL_TMPL: dict[str, str] = {
    "apt-get": "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}",
    "dnf": "dnf install -y {pkg}",
    "yum": "yum install -y {pkg}",
    "apk": "apk add --no-cache {pkg}",
    "zypper": "zypper --non-interactive install {pkg}",
    "pacman": "pacman -Sy --noconfirm {pkg}",
}

# Matches the shell's "<tool>: command not found" / "<tool>: not found" — captures the
# tool name regardless of the "bash: line N:" / "sh: 1:" prefix the shell prepends.
_NOT_FOUND_RE = re.compile(
    r"([\w.+-]+): (?:command not found|not found)",
    re.IGNORECASE,
)


def _package_for(pkg_manager: str, tool: str) -> str:
    """The install package name for a missing command on a given package manager."""
    return _PKG_NAMES.get(pkg_manager, {}).get(tool, tool)


def install_command_for(vm: dict[str, Any], tool: str) -> str:
    """Build the command that installs ``tool`` on this VM (or '' if no package manager).

    Honours the VM's detected ``sudo_mode``: ``passwordless`` uses ``sudo -n``; ``password``
    pipes the SSH login password to ``sudo -S`` (the password is shell-quoted and fed via
    stdin so it never lands in the process list); ``none`` runs the bare command (works when
    the SSH user is already root)."""
    pm = vm.get("pkg_manager") or ""
    tmpl = _INSTALL_TMPL.get(pm)
    if not tmpl:
        return ""
    cmd = tmpl.format(pkg=_package_for(pm, tool))
    sudo_mode = vm.get("sudo_mode") or ("passwordless" if vm.get("can_sudo") else "none")
    # Operator can forbid sudo on this box. If so, only a root SSH user (sudo_mode "none"
    # but commands already run privileged) can install; otherwise there's no install path.
    if not vm.get("allow_sudo", True):
        sudo_mode = "none"
    if sudo_mode == "passwordless":
        return f"sudo -n sh -c {shlex.quote(cmd)}"
    if sudo_mode == "password":
        password = vm.get("ssh_password") or ""
        if password:
            return f"printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' sh -c {shlex.quote(cmd)}"
        return ""
    return cmd


def _missing_tool(cap) -> str:
    """The name of the command that 'command not found' refers to, or '' if none."""
    blob = f"{getattr(cap, 'stderr', '') or ''}\n{getattr(cap, 'stdout', '') or ''}"
    m = _NOT_FOUND_RE.search(blob)
    return m.group(1) if m else ""


def _missing_tool_hint(vm: dict[str, Any], cap) -> str:
    """If a command failed because a tool isn't installed, return an install hint."""
    tool = _missing_tool(cap)
    if not tool:
        return ""
    install = install_command_for(vm, tool)
    if install:
        return (
            f"'{tool}' is not installed on this sandbox. You can install it, then retry, by "
            f"running this with vm_exec:\n  {install}"
        )
    return (
        f"'{tool}' is not installed on this sandbox and no package manager was detected; "
        "use an alternative tool that IS installed, or ask the user to install it."
    )


def vm_context_hint(vms: list[dict[str, Any]]) -> str:
    """System-prompt text describing the sandboxes available this turn."""
    if not vms:
        return ""
    lines = [
        "SANDBOX TROUBLESHOOTING VMs: you have SSH shell access to the following "
        "dedicated sandbox box(es) sitting inside this workload's network. Use the "
        "`vm_exec` tool to run ANY diagnostic command on one of them to investigate "
        "(they can reach private endpoints the platform cannot). Compose your own "
        "commands using the installed tools listed below. If a command needs a tool that "
        "isn't installed, the platform AUTO-INSTALLS it with the box's package manager "
        "(e.g. apt-get) and retries your command automatically — just read the retry "
        "output. If auto-install is unavailable, the tool result gives you the exact "
        "install command to run yourself.",
    ]
    for vm in vms:
        os_info = vm.get("os_info") or "Linux (run `uname -a` to confirm)"
        caps = ", ".join(vm.get("capabilities", []) or []) or "standard shell utilities"
        mode = "STRICT (mutating commands need approval)" if vm.get("strict_mode") else "autonomous"
        net = f" reaching {vm['vnet_label']}" if vm.get("vnet_label") else ""
        pm = vm.get("pkg_manager") or ""
        if not vm.get("allow_sudo", True):
            sudo = "sudo DISABLED by operator — cannot auto-install"
        elif vm.get("sudo_mode") == "password":
            sudo = "with sudo (via login password)"
        elif vm.get("can_sudo"):
            sudo = "with passwordless sudo"
        else:
            sudo = "WITHOUT sudo — cannot auto-install"
        installer = f" Package manager: {pm} ({sudo})." if pm else ""
        lines.append(
            f"- '{vm.get('display_name')}' (id={vm['id']}){net}: {os_info}. "
            f"Installed: {caps}.{installer} Mode: {mode}."
        )
    if len(vms) > 1:
        lines.append("Pass the vm_id to vm_exec to choose a box; otherwise the first is used.")
    return "\n".join(lines)


def _resolve_vm(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    vms_by_id: dict[str, dict[str, Any]] = config.get("vms_by_id", {})
    vm_id = str(args.get("vm_id") or "").strip()
    if vm_id and vm_id in vms_by_id:
        return vms_by_id[vm_id]
    # Default to the first available sandbox.
    return next(iter(vms_by_id.values()), None)


async def _record_run(config: dict[str, Any], vm: dict[str, Any], command: str, cap, status: str) -> None:
    try:
        from app.core.db import SessionLocal
        from app.models import VmRun

        async with SessionLocal() as db:
            db.add(VmRun(
                vm_id=vm["id"],
                vm_name=vm.get("display_name"),
                tenant_id=config.get("tenant_id", ""),
                command=command,
                destructive=getattr(cap, "destructive", False),
                status=status,
                exit_code=getattr(cap, "exit_code", None),
                output=(getattr(cap, "stdout", "") or "")[:200_000] or None,
                stderr=(getattr(cap, "stderr", "") or "")[:8000] or None,
                trigger=config.get("trigger", "chat"),
                chat_id=config.get("chat_id"),
                triggered_by=config.get("actor", ""),
                error=getattr(cap, "error", "") or None,
                duration_ms=getattr(cap, "duration_ms", None),
            ))
            await db.commit()
    except Exception:  # noqa: BLE001 - never let history logging break a tool call
        logger.debug("VmRun logging failed", exc_info=True)


def _redact_secrets(text: str, vm: dict[str, Any]) -> str:
    """Mask the VM's SSH password/passphrase anywhere they appear in text we display or log."""
    if not text:
        return text
    for f in ("ssh_password", "ssh_passphrase"):
        secret = vm.get(f) or ""
        if secret and secret in text:
            text = text.replace(secret, "••••")
    return text


def _render_capture(command: str, vm: dict[str, Any], cap) -> list[str]:
    """Render a single capture as the STDOUT/STDERR/NOTE blocks for the tool result."""
    parts: list[str] = [f"$ {command}  (on '{vm.get('display_name')}', exit={cap.exit_code})"]
    if cap.stdout:
        parts.append("STDOUT:\n" + cap.stdout)
    if cap.stderr:
        parts.append("STDERR:\n" + cap.stderr)
    if cap.error:
        parts.append("NOTE: " + cap.error)
    return parts


async def _vm_exec(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if not load_settings().get("sandbox_tools_enabled", True):
        return err("Sandbox VM tools are disabled by the administrator.")
    vm = _resolve_vm(config, args)
    if vm is None:
        return err("No sandbox VM is available for this workload.")
    command = str(args.get("command") or "").strip()
    if not command:
        return err("A 'command' is required.")
    read_only = bool(config.get("read_only"))
    # Deep-investigation / read-only contexts refuse mutating commands outright.
    if read_only and is_destructive(command):
        return err(
            "This appears to modify the system; only read-only diagnostic commands are "
            "allowed in this mode. Run a non-mutating command instead."
        )
    cap = await run_ssh_capture(vm, command)
    if cap.needs_approval:
        await _record_run(config, vm, command, cap, "blocked")
        return err(
            "This command may modify the sandbox and the VM is in strict mode — it needs "
            "operator approval before it can run. Try a read-only diagnostic instead, or "
            "ask the user to approve the change."
        )

    # Infrastructure failure: the box never ran the command (SSH connection refused,
    # host unreachable, auth/host-key error, or a connect/exec timeout). The command
    # never executes, so exit_code stays None and there's no stdout/stderr — only an
    # error. Surface this PROMINENTLY and distinctly from "command ran but failed", so a
    # dead sandbox doesn't masquerade as a successful diagnostic.
    if cap.exit_code is None and cap.error and not cap.stdout and not cap.stderr:
        name = vm.get("display_name") or vm.get("id") or "sandbox VM"
        timed_out = "timed out" in (cap.error or "").lower()
        await _record_run(config, vm, command, cap, "timeout" if timed_out else "failed")
        detail = _redact_secrets(cap.error.strip(), vm)
        return err(
            f"Sandbox VM '{name}' (host {vm.get('host') or '?'}) is unavailable — the "
            f"command did NOT run. {detail} "
            "Check the VM is powered on, reachable from this app, and that its SSH "
            "credentials/host key are still valid (re-test it under Settings → Sandbox "
            "VMs), then retry."
        )

    status = "succeeded" if cap.ok else ("timeout" if "timed out" in (cap.error or "").lower() else "failed")
    await _record_run(config, vm, command, cap, status)

    parts: list[str] = _render_capture(command, vm, cap)

    # Auto-install + retry: if the command failed only because a diagnostic tool isn't
    # installed, install it with the box's package manager (these are disposable sandboxes)
    # and re-run the original command once. Disabled in read-only/deep mode and gated by
    # the `sandbox_auto_install` setting; requires a known install command for the box.
    auto = load_settings().get("sandbox_auto_install", True)
    tool = "" if read_only or not auto else _missing_tool(cap)
    install = install_command_for(vm, tool) if tool else ""
    if install and not is_destructive(command):
        shown = _redact_secrets(install, vm)
        parts.append(f"AUTO-INSTALL — '{tool}' was missing; installing it on the sandbox…")
        icap = await run_ssh_capture(vm, install)
        await _record_run(config, vm, shown, icap, "succeeded" if icap.ok else "failed")
        if icap.ok:
            parts.append(f"$ {shown}\n→ installed OK; retrying the original command.")
            rcap = await run_ssh_capture(vm, command)
            rstatus = "succeeded" if rcap.ok else ("timeout" if "timed out" in (rcap.error or "").lower() else "failed")
            await _record_run(config, vm, command, rcap, rstatus)
            parts.append("RETRY after install:")
            parts.extend(_render_capture(command, vm, rcap))
            cap = rcap  # final result reflects the retry
        else:
            ierr = (icap.stderr or icap.error or "install failed").strip()
            parts.append(_redact_secrets(
                f"Auto-install of '{tool}' failed (exit={icap.exit_code}): {ierr[:600]}\n"
                + _missing_tool_hint(vm, cap), vm
            ))
    elif not read_only:
        # No auto-install (off, no package manager, or destructive original) — fall back to
        # telling the model exactly how to install the tool itself.
        hint = _missing_tool_hint(vm, cap)
        if hint:
            parts.append("TOOL MISSING — " + _redact_secrets(hint, vm))

    if cap.error and not cap.stdout and not cap.stderr and len(parts) == 1:
        return err(cap.error)
    text = "\n\n".join(parts)
    if len(text) > _MAX_TOOL_OUTPUT:
        text = text[:_MAX_TOOL_OUTPUT] + "\n…[truncated]"
    return ok(text)


async def _vm_list(config: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
    vms_by_id: dict[str, dict[str, Any]] = config.get("vms_by_id", {})
    if not vms_by_id:
        return ok("No sandbox VMs are available for this workload.")
    lines = []
    for vm in vms_by_id.values():
        caps = ", ".join(vm.get("capabilities", []) or []) or "unknown"
        lines.append(
            f"- id={vm['id']} '{vm.get('display_name')}' @ {vm.get('host')} — "
            f"{vm.get('os_info') or 'OS unknown'} — tools: {caps} — "
            f"mode: {'strict' if vm.get('strict_mode') else 'autonomous'}"
        )
    return ok("Available sandbox VMs:\n" + "\n".join(lines))


async def _vm_read_file(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    vm = _resolve_vm(config, args)
    if vm is None:
        return err("No sandbox VM is available for this workload.")
    path = str(args.get("path") or "").strip()
    if not path:
        return err("A 'path' is required.")
    # Quote the path to avoid injection; cat is read-only.
    safe = path.replace("'", "'\\''")
    cap = await run_ssh_capture(vm, f"cat -- '{safe}'")
    await _record_run(config, vm, f"cat {path}", cap, "succeeded" if cap.ok else "failed")
    if not cap.ok and not cap.stdout:
        return err(cap.error or cap.stderr or f"Could not read {path}.")
    body = cap.stdout
    if len(body) > _MAX_TOOL_OUTPUT:
        body = body[:_MAX_TOOL_OUTPUT] + "\n…[truncated]"
    return ok(f"{path} on '{vm.get('display_name')}':\n{body}")


def _tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="vm_exec",
            description=(
                "Run a shell command on an onboarded sandbox troubleshooting VM and return "
                "its output. The VM sits inside the workload's network and can reach private "
                "endpoints. Use the OS + installed tools described in the system prompt to "
                "compose commands (e.g. dig, curl -v, nc -vz host port, mtr, ip route, ss). "
                "Optionally pass vm_id to pick a specific box."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run on the VM."},
                    "vm_id": {"type": "string", "description": "Which sandbox VM to use (optional; defaults to the first)."},
                },
                "required": ["command"],
            },
            kind="read",  # classification is dynamic; strict-mode gating happens in the runner
            handler=_vm_exec,
        ),
        ConnectorTool(
            name="vm_list",
            description="List the sandbox troubleshooting VMs available for this workload, with their OS and installed tools.",
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_vm_list,
        ),
        ConnectorTool(
            name="vm_read_file",
            description="Read a text file from a sandbox VM (read-only). Optionally pass vm_id.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to read."},
                    "vm_id": {"type": "string", "description": "Which sandbox VM (optional)."},
                },
                "required": ["path"],
            },
            kind="read",
            handler=_vm_read_file,
        ),
    ]


def register_vm_tools(
    toolset: ConnectorToolset,
    vms: list[dict[str, Any]],
    *,
    tenant_id: str = "",
    chat_id: str | None = None,
    actor: str = "",
    trigger: str = "chat",
    read_only: bool = False,
) -> None:
    """Add vm_exec/vm_list/vm_read_file to an existing toolset, bound to ``vms``.

    Does nothing if there are no VMs or the admin kill-switch is off."""
    if not vms or not load_settings().get("sandbox_tools_enabled", True):
        return
    config = {
        "vms_by_id": {vm["id"]: vm for vm in vms},
        "tenant_id": tenant_id,
        "chat_id": chat_id,
        "actor": actor,
        "trigger": trigger,
        "read_only": read_only,
        "_resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    toolset.add_connector(config, _tools())
