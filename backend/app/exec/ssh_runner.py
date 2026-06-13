"""SSH command execution on onboarded sandbox VMs (asyncssh).

Parallels :mod:`app.exec.command_runner` but targets remote sandbox boxes over SSH
instead of the local host. The agent uses these boxes as diagnostic probes that sit
inside a workload's VNet, so commands run FROM the box can reach private endpoints.

Safety model (sandbox posture):
- These are dedicated, low-blast-radius troubleshooting boxes, so the DEFAULT is to run
  any command autonomously and stream the output back.
- Destructive commands are still detected (reusing the command_runner mutating-verb
  classifier). When a VM is in ``strict_mode`` a destructive command is NOT auto-run;
  the caller surfaces it for approval.
- Host-key trust uses TOFU: the fingerprint is pinned on the first successful Test and a
  later mismatch is rejected (MITM guard).
- The private key is loaded in memory only (never written to disk). Output is bounded;
  a concurrency semaphore caps simultaneous SSH sessions; commands have a timeout.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import Any, AsyncIterator

import asyncssh

from app.core.app_settings import load_settings
from app.exec.command_runner import MAX_OUTPUT_BYTES, _MUTATING_VERBS

logger = logging.getLogger("app.exec.ssh_runner")

# Cap simultaneous SSH sessions so a fan-out of vm_exec calls can't exhaust the host.
_SSH_SEMAPHORE = asyncio.Semaphore(4)

# Tools we probe for during environment detection (informs the LLM what it can use).
_PROBE_TOOLS = (
    "bash", "sh", "dig", "nslookup", "host", "curl", "wget", "nc", "ncat", "socat",
    "ping", "traceroute", "tracepath", "mtr", "tcpdump", "ss", "netstat", "ip",
    "ifconfig", "route", "arp", "openssl", "jq", "python3", "az", "kubectl", "psql",
    "mysql", "redis-cli", "nmap", "telnet", "whois", "resolvectl",
)


def is_destructive(command: str) -> bool:
    """True if a command contains a mutating verb (best-effort, reuses az/kubectl set).

    Shell pipelines/operators are allowed on a sandbox, so we tokenize loosely and look
    for any bare mutating verb token. Errs toward marking destructive on parse failure
    of a suspicious string."""
    raw = (command or "").strip()
    if not raw:
        return False
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        # Unparseable (unbalanced quotes) — be conservative.
        return True
    bare = {t.lower() for t in tokens if t and not t.startswith("-")}
    # Common destructive shell builtins/binaries on top of the cloud-CLI verb set.
    shell_destructive = {
        "rm", "rmdir", "mv", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
        "kill", "killall", "pkill", "chmod", "chown", "chattr", "truncate", "tee",
        "iptables", "ufw", "systemctl", "service", "apt", "apt-get", "yum", "dnf",
        "pip", "npm", "useradd", "userdel", "passwd", "mount", "umount", "fdisk",
        "parted", "crontab", "kubeadm",
    }
    return bool(bare & (_MUTATING_VERBS | shell_destructive))


async def _connect(vm: dict[str, Any], *, known_fingerprint: str = "") -> asyncssh.SSHClientConnection:
    """Open an SSH connection to a sandbox VM. Enforces host-key TOFU when a fingerprint
    is already pinned. The private key is parsed from memory."""
    host = vm.get("host", "")
    port = int(vm.get("port", 22) or 22)
    username = vm.get("username", "")
    if not host or not username:
        raise ValueError("Sandbox VM is missing host or username.")

    conn_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        # We do our own TOFU check against the server key below, so disable asyncssh's
        # known_hosts file lookup (there isn't one for a service account).
        "known_hosts": None,
    }
    if vm.get("auth_method") == "ssh_key" and vm.get("ssh_private_key"):
        try:
            key = asyncssh.import_private_key(
                vm["ssh_private_key"], passphrase=vm.get("ssh_passphrase") or None
            )
        except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError) as exc:
            raise ValueError(f"Could not load the SSH private key: {exc}") from exc
        conn_kwargs["client_keys"] = [key]
    else:
        if not vm.get("ssh_password"):
            raise ValueError("Sandbox VM has no password or private key configured.")
        conn_kwargs["password"] = vm["ssh_password"]

    conn = await asyncssh.connect(**conn_kwargs)
    # TOFU: compare the presented host key against the pinned fingerprint.
    try:
        server_key = conn.get_server_host_key()
        presented = server_key.get_fingerprint() if server_key else ""
    except Exception:  # noqa: BLE001
        presented = ""
    if known_fingerprint and presented and presented != known_fingerprint:
        conn.close()
        raise PermissionError(
            "Host key mismatch — the sandbox VM's SSH fingerprint changed since it was "
            "onboarded. Connection refused (possible man-in-the-middle). Re-test the VM "
            "to re-pin if this change was expected."
        )
    # Stash the presented fingerprint so callers (Test) can pin it.
    setattr(conn, "_presented_fingerprint", presented)
    return conn


async def detect_environment(vm: dict[str, Any]) -> dict[str, Any]:
    """Connect, identify the OS, and probe the installed toolkit.

    Returns ``{ok, os_info, capabilities, fingerprint, whoami, error}``. Pins nothing;
    the caller decides whether to persist."""
    async with _SSH_SEMAPHORE:
        try:
            conn = await asyncio.wait_for(
                _connect(vm, known_fingerprint=vm.get("host_key_fingerprint", "")),
                timeout=20,
            )
        except (OSError, asyncssh.Error, ValueError, PermissionError, asyncio.TimeoutError) as exc:
            return {"ok": False, "error": str(exc), "os_info": "", "capabilities": [], "pkg_manager": "", "can_sudo": False, "sudo_mode": "none", "fingerprint": "", "whoami": ""}
        fingerprint = getattr(conn, "_presented_fingerprint", "")
        try:
            whoami_r = await conn.run("whoami", check=False, timeout=10)
            whoami = (whoami_r.stdout or "").strip()
            # OS string: prefer PRETTY_NAME from os-release, append kernel.
            os_r = await conn.run(
                "(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\"); uname -sr -m",
                check=False, timeout=10,
            )
            os_lines = [ln.strip() for ln in (os_r.stdout or "").splitlines() if ln.strip()]
            os_info = " · ".join(os_lines) if os_lines else "unknown"
            # Toolkit probe: one `command -v` per tool, collected in a single round-trip.
            probe = "; ".join(f"command -v {t} >/dev/null 2>&1 && echo {t}" for t in _PROBE_TOOLS)
            tools_r = await conn.run(probe, check=False, timeout=15)
            caps = [ln.strip() for ln in (tools_r.stdout or "").splitlines() if ln.strip()]
            # Package manager + sudo probe, so the agent can install missing tools on
            # demand. First matching manager wins. We detect THREE sudo states:
            #   - "passwordless": `sudo -n` works (no password prompt)
            #   - "password": sudo works when the login password is piped via `sudo -S`
            #   - "none": the user can't sudo at all
            # `can_sudo` is true for either working mode (the agent has the password and can
            # run installs unattended in both).
            pm_probe = (
                "for m in apt-get dnf yum apk zypper pacman; do "
                "command -v $m >/dev/null 2>&1 && { echo PM=$m; break; }; done; "
                "(sudo -n true >/dev/null 2>&1 && echo SUDO=passwordless) || echo SUDO=no"
            )
            pm_r = await conn.run(pm_probe, check=False, timeout=10)
            pkg_manager = ""
            sudo_mode = "none"
            for ln in (pm_r.stdout or "").splitlines():
                ln = ln.strip()
                if ln.startswith("PM="):
                    pkg_manager = ln[3:]
                elif ln == "SUDO=passwordless":
                    sudo_mode = "passwordless"
            # If passwordless isn't available, try password-sudo using the SSH login
            # password (only meaningful for password auth). We pipe it via stdin to
            # `sudo -S` so it never appears in the process list.
            password = vm.get("ssh_password") or ""
            if sudo_mode == "none" and password:
                try:
                    pw_probe = f"printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' true >/dev/null 2>&1 && echo OKSUDO"
                    pw_r = await conn.run(pw_probe, check=False, timeout=10)
                    if "OKSUDO" in (pw_r.stdout or ""):
                        sudo_mode = "password"
                except (asyncssh.Error, asyncio.TimeoutError):
                    pass
            can_sudo = sudo_mode != "none"
            return {
                "ok": True, "os_info": os_info, "capabilities": caps,
                "pkg_manager": pkg_manager, "can_sudo": can_sudo, "sudo_mode": sudo_mode,
                "fingerprint": fingerprint, "whoami": whoami, "error": "",
            }
        finally:
            conn.close()


async def run_ssh_stream(
    vm: dict[str, Any], command: str, *, confirm: bool = False
) -> AsyncIterator[dict[str, Any]]:
    """Run a command on the sandbox VM and yield SSE-ready events.

    Event shapes mirror command_runner: ``exec_start``, ``stdout``, ``stderr``,
    ``approval_required``, ``exit`` (code/duration_ms), ``error``."""
    settings = load_settings()
    if not settings.get("sandbox_tools_enabled", True):
        yield {"type": "error", "message": "Sandbox VM tools are disabled by the administrator."}
        return
    cmd = (command or "").strip()
    if not cmd:
        yield {"type": "error", "message": "Empty command."}
        return
    if len(cmd) > 8000:
        yield {"type": "error", "message": "Command is too long."}
        return

    destructive = is_destructive(cmd)
    # Strict-mode VMs require approval for destructive commands.
    if destructive and vm.get("strict_mode") and not confirm:
        yield {
            "type": "approval_required",
            "command": cmd,
            "message": "This command may modify the sandbox VM. Run it anyway?",
        }
        return

    timeout = int(settings.get("sandbox_command_timeout_seconds", 60) or 60)
    yield {"type": "exec_start", "command": cmd, "destructive": destructive}
    started = time.monotonic()

    async with _SSH_SEMAPHORE:
        try:
            conn = await asyncio.wait_for(
                _connect(vm, known_fingerprint=vm.get("host_key_fingerprint", "")),
                timeout=20,
            )
        except (OSError, asyncssh.Error, ValueError, PermissionError, asyncio.TimeoutError) as exc:
            yield {"type": "error", "message": f"SSH connection failed: {exc}"}
            return
        try:
            try:
                result = await asyncio.wait_for(
                    conn.run(cmd, check=False), timeout=timeout
                )
            except asyncio.TimeoutError:
                yield {"type": "error", "message": f"Command timed out after {timeout}s."}
                return
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            if len(stdout) > MAX_OUTPUT_BYTES:
                stdout = stdout[:MAX_OUTPUT_BYTES] + "\n…[truncated]"
            if len(stderr) > 8000:
                stderr = stderr[:8000] + "\n…[truncated]"
            if stdout:
                yield {"type": "stdout", "text": stdout}
            if stderr:
                yield {"type": "stderr", "text": stderr}
            duration_ms = int((time.monotonic() - started) * 1000)
            yield {"type": "exit", "code": result.exit_status, "duration_ms": duration_ms}
        finally:
            conn.close()


class SshCapture:
    """The full, captured result of a (non-streamed) SSH command run."""

    def __init__(self) -> None:
        self.ok = False
        self.stdout = ""
        self.stderr = ""
        self.exit_code: int | None = None
        self.duration_ms: int | None = None
        self.error = ""
        self.destructive = False
        self.needs_approval = False


async def run_ssh_capture(vm: dict[str, Any], command: str, *, confirm: bool = False) -> SshCapture:
    """Drain :func:`run_ssh_stream` into a single captured result."""
    res = SshCapture()
    async for ev in run_ssh_stream(vm, command, confirm=confirm):
        kind = ev.get("type")
        if kind == "exec_start":
            res.destructive = bool(ev.get("destructive"))
        elif kind == "stdout":
            res.stdout += ev.get("text", "")
        elif kind == "stderr":
            res.stderr += ev.get("text", "")
        elif kind == "approval_required":
            res.needs_approval = True
            res.error = "This command requires approval (sandbox is in strict mode)."
            return res
        elif kind == "error":
            res.error = ev.get("message", "Command failed.")
        elif kind == "exit":
            res.exit_code = ev.get("code")
            res.duration_ms = ev.get("duration_ms")
    if res.error:
        res.ok = False
    else:
        res.ok = res.exit_code == 0
    return res
