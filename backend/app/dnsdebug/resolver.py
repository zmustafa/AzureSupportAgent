"""Live DNS resolution-chain probe, executed via vm_exec on a sandbox VM.

Runs an ordered set of read-only DNS diagnostics from a box in (or peered to) the source
VNet: effective DNS server → resolver chosen → resolve the FQDN (+ CNAME trace) → returned
IP → public/private classification, plus a hosts-file shadow check. Each step yields a
structured result for the UI chain animation and the analyzer.

Honors the ``sandbox_tools_enabled`` kill-switch + per-VM strict-mode (via run_ssh_capture)
and auto-installs missing tools (dig/host) when allowed."""
from __future__ import annotations

import ipaddress
import logging
import re
import shlex
from typing import Any, AsyncIterator

log = logging.getLogger("app.dnsdebug.resolver")

STEP_EFFECTIVE_DNS = "effective_dns"
STEP_RESOLVER = "resolver"
STEP_RESOLVE = "resolve"
STEP_CNAME = "cname"
STEP_CLASSIFY = "classify"
STEP_HOSTS = "hosts"

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_SKIP = "skip"

AZURE_DNS = "168.63.129.16"


def _q(s: str) -> str:
    return shlex.quote(str(s or ""))


def _is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def _first_ip(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", line):
            return line
    m = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text or "")
    return m.group(0) if m else ""


def _parse_resolvers(text: str) -> list[str]:
    """DNS servers from resolvectl/resolv.conf output."""
    servers: list[str] = []
    for m in re.finditer(r"(?:DNS Servers?:|nameserver)\s+([0-9.]+)", text or ""):
        ip = m.group(1)
        if ip not in servers:
            servers.append(ip)
    return servers


def _build_command(step: str, *, fqdn: str) -> str:
    f = _q(fqdn)
    if step == STEP_EFFECTIVE_DNS:
        return "(resolvectl status 2>/dev/null || systemd-resolve --status 2>/dev/null); echo '---'; cat /etc/resolv.conf 2>/dev/null"
    if step == STEP_RESOLVE:
        return f"dig +tries=1 +time=3 +short {f}; echo '---ANSWER---'; dig +tries=1 +time=3 {f} 2>/dev/null | sed -n '/ANSWER SECTION/,/^$/p'"
    if step == STEP_CNAME:
        return f"dig +tries=1 +time=3 {f} CNAME +short; echo '---TRACE---'; dig +trace +tries=1 +time=3 {f} 2>/dev/null | grep -E 'CNAME|A\\s' | head -20"
    if step == STEP_HOSTS:
        return f"getent hosts {f}; echo '---'; grep -i {f} /etc/hosts 2>/dev/null || echo 'no hosts entry'"
    return "true"


def _classify_step(step: str, cap, *, fqdn: str, state: dict[str, Any]) -> tuple[str, str]:
    out = ((cap.stdout or "") + "\n" + (cap.stderr or "")).strip()
    if step == STEP_EFFECTIVE_DNS:
        servers = _parse_resolvers(out)
        state["resolvers"] = servers
        if not servers:
            return STATUS_WARN, "Could not read effective DNS servers"
        custom = [s for s in servers if s != AZURE_DNS and not s.startswith("127.")]
        state["custom_dns"] = custom
        if custom:
            return STATUS_WARN, f"Custom DNS server(s): {', '.join(servers)}"
        return STATUS_OK, f"DNS server(s): {', '.join(servers)}"
    if step == STEP_RESOLVE:
        ip = _first_ip(out)
        state["resolved_ip"] = ip
        if not ip:
            return STATUS_FAIL, "FQDN did not resolve to any A record"
        return STATUS_OK, f"Resolved to {ip}"
    if step == STEP_CNAME:
        has_pl = "privatelink" in out.lower()
        state["cname_to_privatelink"] = has_pl
        if has_pl:
            return STATUS_OK, "CNAME chain reaches privatelink.* (expected for PE)"
        return STATUS_WARN, "No privatelink CNAME in the chain"
    if step == STEP_HOSTS:
        if "no hosts entry" in out.lower():
            return STATUS_OK, "No /etc/hosts shadow entry"
        if _first_ip(out):
            return STATUS_WARN, "A /etc/hosts entry may be shadowing DNS"
        return STATUS_OK, "No hosts shadow"
    return STATUS_SKIP, ""


async def run_resolution(
    vm: dict[str, Any],
    *,
    fqdn: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield {type:'step', step, status, evidence, command, raw, duration_ms} per step, plus
    a final {type:'state', ...} carrying the aggregated resolver/resolved_ip/classification."""
    from app.agent.vm_tools import install_command_for
    from app.core.app_settings import load_settings
    from app.exec.ssh_runner import run_ssh_capture

    if not load_settings().get("sandbox_tools_enabled", True):
        yield {"type": "step", "step": "gate", "status": STATUS_FAIL,
               "evidence": "Sandbox VM tools are disabled by the administrator.", "command": "", "raw": "", "duration_ms": 0}
        return

    state: dict[str, Any] = {"resolvers": [], "custom_dns": [], "resolved_ip": "", "cname_to_privatelink": False}
    caps = set(vm.get("capabilities") or [])
    steps = [STEP_EFFECTIVE_DNS, STEP_RESOLVE, STEP_CNAME, STEP_HOSTS]

    for step in steps:
        command = _build_command(step, fqdn=fqdn)
        cap = await run_ssh_capture(vm, command)
        if "dig" not in caps and "command not found" in ((cap.stdout or "") + (cap.stderr or "")).lower():
            install = install_command_for(vm, "dig")
            if install and load_settings().get("sandbox_auto_install", True):
                await run_ssh_capture(vm, install)
                cap = await run_ssh_capture(vm, command)
        status, evidence = _classify_step(step, cap, fqdn=fqdn, state=state)
        raw = ((cap.stdout or "") + ("\n" + cap.stderr if cap.stderr else "")).strip()
        if cap.needs_approval:
            status, evidence = STATUS_WARN, "Command requires approval (sandbox strict mode)."
        yield {"type": "step", "step": step, "status": status, "evidence": evidence,
               "command": command, "raw": raw[:4000], "duration_ms": cap.duration_ms or 0}

    # Classification step derived from resolved IP.
    ip = state.get("resolved_ip", "")
    if ip:
        private = _is_private_ip(ip)
        state["is_private"] = private
        yield {"type": "step", "step": STEP_CLASSIFY,
               "status": STATUS_OK if private else STATUS_FAIL,
               "evidence": f"{ip} is a {'PRIVATE' if private else 'PUBLIC'} IP",
               "command": "", "raw": "", "duration_ms": 0}
    else:
        state["is_private"] = None
        yield {"type": "step", "step": STEP_CLASSIFY, "status": STATUS_FAIL,
               "evidence": "No IP to classify", "command": "", "raw": "", "duration_ms": 0}

    yield {"type": "state", **state}
