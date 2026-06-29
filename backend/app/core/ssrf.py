"""Central SSRF guard for admin-supplied outbound URLs.

Several admin-only features make the server fetch/POST to an operator-provided URL
(LLM provider ``base_url``, the generic webhook and Teams connectors). Without a check
those become an SSRF pivot into the internal network or cloud metadata. This module
validates the scheme and resolves the host, refusing private / loopback / link-local /
metadata addresses. It mirrors the agent tool guard (app.agent.builtins) so all outbound
URL paths share one policy.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if str(addr) in ("169.254.169.254", "fd00:ec2::254"):  # cloud metadata
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def check_url(url: str, *, require_https: bool = False, allow_private: bool = False) -> str | None:
    """Return an error string if the URL must not be contacted, else ``None``.

    ``allow_private`` permits loopback/private targets (for local providers like Ollama)
    but the cloud metadata endpoint is ALWAYS blocked.
    """
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return "Malformed URL."
    if p.scheme not in ("http", "https"):
        return "Only http and https URLs are allowed."
    if require_https and p.scheme != "https":
        return "Only https URLs are allowed."
    host = p.hostname
    if not host:
        return "URL has no host."
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = sorted({i[4][0] for i in infos})
    except socket.gaierror as exc:
        return f"DNS resolution failed for '{host}': {exc}"
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return f"Refusing to contact '{host}' — unparseable address."
        if str(addr) in ("169.254.169.254", "fd00:ec2::254"):
            return f"Refusing to contact '{host}' — cloud metadata address is blocked."
        if not allow_private and _ip_is_blocked(ip):
            return f"Refusing to contact '{host}' — resolves to a private/blocked address ({ip})."
    return None

