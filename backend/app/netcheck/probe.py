"""Live reachability probe sequence, executed via vm_exec on a sandbox VM.

Builds and runs an ordered set of diagnostic commands over SSH (run_ssh_capture) from a
sandbox box that can reach the target's private network: DNS resolve → ICMP → TCP connect
→ TLS handshake → HTTP probe (each conditional on protocol/payload). Every step yields a
structured result so the UI can animate it and the analyzer can reason over it.

Honors the ``sandbox_tools_enabled`` kill-switch and per-VM strict-mode approval (via
run_ssh_capture). Missing diagnostic tools are auto-installed when the box allows it
(mirrors app.agent.vm_tools)."""
from __future__ import annotations

import logging
import re
import shlex
from typing import Any, AsyncIterator

log = logging.getLogger("app.netcheck.probe")

# Probe step identifiers (stable; the UI maps these to row labels + icons).
STEP_DNS = "dns"
STEP_ICMP = "icmp"
STEP_TCP = "tcp"
STEP_TLS = "tls"
STEP_HTTP = "http"

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_SKIP = "skip"


def _q(s: str) -> str:
    return shlex.quote(str(s or ""))


def _tool_for_step(step: str) -> str:
    return {STEP_DNS: "dig", STEP_ICMP: "ping", STEP_TCP: "nc", STEP_TLS: "openssl", STEP_HTTP: "curl"}.get(step, "")


def _looks_like_ip(host: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host or ""))


def _plan_steps(*, protocol: str, port: int, payload: dict[str, Any] | None) -> list[str]:
    """Decide which probe steps to run for this request."""
    proto = (protocol or "tcp").lower()
    steps = [STEP_DNS, STEP_ICMP, STEP_TCP]
    payload = payload or {}
    if proto in ("tls", "https") or payload.get("sni") or int(port or 0) == 443:
        steps.append(STEP_TLS)
    if proto in ("http", "https") or payload.get("http_path"):
        steps.append(STEP_HTTP)
    return steps


def _build_command(step: str, *, target: str, port: int, payload: dict[str, Any]) -> str:
    """The shell command for one probe step (read-only diagnostics only)."""
    t = _q(target)
    p = int(port or 0)
    if step == STEP_DNS:
        # +short for the answer; also capture the server that answered for private-DNS proof.
        return f"dig +tries=1 +time=2 +short {t} A; echo '---'; dig +tries=1 +time=2 {t} A | grep -E 'SERVER:|ANSWER SECTION' || true"
    if step == STEP_ICMP:
        return f"ping -c 3 -W 2 {t}"
    if step == STEP_TCP:
        return f"nc -vz -w 5 {t} {p} 2>&1"
    if step == STEP_TLS:
        sni = payload.get("sni") or target
        return f"echo | timeout 8 openssl s_client -connect {t}:{p} -servername {_q(sni)} 2>/dev/null | openssl x509 -noout -subject -issuer -dates 2>/dev/null || echo 'TLS handshake failed'"
    if step == STEP_HTTP:
        path = payload.get("http_path") or "/"
        scheme = "https" if (p == 443 or (payload.get("sni"))) else "http"
        host_hdr = payload.get("host") or target
        url = f"{scheme}://{target}:{p}{path}"
        return f"curl -sS -m 8 -o /dev/null -D - -H {_q('Host: ' + host_hdr)} {_q(url)} 2>&1 | head -20 || echo 'HTTP probe failed'"
    return "true"


def _classify(step: str, cap) -> tuple[str, str]:
    """Map a captured result to (status, evidence) for one step."""
    out = (cap.stdout or "") + (cap.stderr or "")
    low = out.lower()
    if step == STEP_DNS:
        ip = ""
        for line in (cap.stdout or "").splitlines():
            line = line.strip()
            if _looks_like_ip(line):
                ip = line
                break
        if ip:
            return STATUS_OK, f"Resolved to {ip}"
        return STATUS_FAIL, "DNS did not resolve (no A record / private zone miss)"
    if step == STEP_ICMP:
        if "100% packet loss" in low:
            return STATUS_WARN, "ICMP blocked (common; not conclusive — see TCP)"
        if "0% packet loss" in low:
            return STATUS_OK, "ICMP reachable (0% loss)"
        return STATUS_WARN, "ICMP inconclusive"
    if step == STEP_TCP:
        if "succeeded" in low or "open" in low or "connected" in low:
            return STATUS_OK, "TCP connect succeeded"
        if "timed out" in low or "timeout" in low:
            return STATUS_FAIL, "TCP connect timed out (likely NSG/firewall/route block)"
        if "refused" in low:
            return STATUS_FAIL, "TCP connection refused (port closed / no listener)"
        return STATUS_FAIL, "TCP connect failed"
    if step == STEP_TLS:
        if "subject=" in low and "failed" not in low:
            subj = ""
            for line in out.splitlines():
                if line.lower().startswith("subject="):
                    subj = line.strip()
                    break
            return STATUS_OK, f"TLS handshake OK ({subj[:80]})" if subj else "TLS handshake OK"
        return STATUS_FAIL, "TLS handshake failed"
    if step == STEP_HTTP:
        m = re.search(r"HTTP/\d(?:\.\d)?\s+(\d{3})", out)
        if m:
            code = int(m.group(1))
            status = STATUS_OK if code < 400 else (STATUS_WARN if code < 500 else STATUS_FAIL)
            return status, f"HTTP {code}"
        return STATUS_FAIL, "No HTTP response"
    return STATUS_SKIP, ""


async def run_probe(
    vm: dict[str, Any],
    *,
    target: str,
    port: int,
    protocol: str,
    payload: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one event per probe step: {type:'step', step, status, evidence, command, raw,
    duration_ms}. Ends with no terminal event (the caller aggregates)."""
    from app.agent.vm_tools import install_command_for
    from app.core.app_settings import load_settings
    from app.exec.ssh_runner import run_ssh_capture

    payload = payload or {}
    if not load_settings().get("sandbox_tools_enabled", True):
        yield {"type": "step", "step": "gate", "status": STATUS_FAIL,
               "evidence": "Sandbox VM tools are disabled by the administrator.", "command": "", "raw": "", "duration_ms": 0}
        return

    steps = _plan_steps(protocol=protocol, port=port, payload=payload)
    caps = set((vm.get("capabilities") or []))
    pkg = vm.get("pkg_manager", "")

    for step in steps:
        command = _build_command(step, target=target, port=port, payload=payload)
        cap = await run_ssh_capture(vm, command)

        # Auto-install a missing tool once, then retry (mirrors vm_tools behavior).
        tool = _tool_for_step(step)
        if tool and tool not in caps and ("command not found" in ((cap.stdout or "") + (cap.stderr or "")).lower()):
            install = install_command_for(vm, tool)
            if install and load_settings().get("sandbox_auto_install", True):
                await run_ssh_capture(vm, install)
                cap = await run_ssh_capture(vm, command)

        status, evidence = _classify(step, cap)
        raw = ((cap.stdout or "") + ("\n" + cap.stderr if cap.stderr else "")).strip()
        if cap.needs_approval:
            status, evidence = STATUS_WARN, "Command requires approval (sandbox strict mode)."
        yield {
            "type": "step",
            "step": step,
            "status": status,
            "evidence": evidence,
            "command": command,
            "raw": raw[:4000],
            "duration_ms": cap.duration_ms or 0,
        }
