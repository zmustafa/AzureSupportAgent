"""Resolution of the originating client IP from a request.

Shared by the login throttle, the audit log and the network-access middleware, so there is
exactly ONE definition of "who is calling" in the application.

SECURITY — why this walks the header from the RIGHT (measured 2026-08-04)
------------------------------------------------------------------------
``X-Forwarded-For`` is ``<client-supplied entries…>, <appended by each proxy>``. A proxy
APPENDS the peer it received the request from; it does not replace what was already there.
So anything the caller injects is always to the LEFT of the address the proxy vouches for.

This was verified empirically against the deployed Azure Container Apps ingress: a request
carrying ``X-Forwarded-For: 203.0.113.77`` was recorded by the application as coming from
``203.0.113.77``. Reading the LEFTMOST entry therefore let any caller choose their own
apparent IP — evading the per-IP brute-force counter and poisoning audit records.

Walking right-to-left and taking the first *globally routable* address fixes that:

* injected entries are always further left, so they can never win;
* private / loopback / link-local entries are skipped rather than counted, so an unknown or
  changing number of internal proxy hops does not need to be configured, and a platform that
  adds a hop later does not silently break the result.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from app.core.config import get_settings

# Ranges that can only be infrastructure, never an originating internet client. Used to skip
# proxy hops while walking the forwarded header from the right.
#
# Deliberately NOT expressed as `not ip.is_global`: Python classifies the RFC 5737 / RFC 3849
# DOCUMENTATION ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) as
# private, so `is_global` would discard exactly the addresses every test fixture and worked
# example uses. This list names the internal ranges explicitly instead.
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",       # RFC 1918
        "172.16.0.0/12",    # RFC 1918
        "192.168.0.0/16",   # RFC 1918
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local
        "100.64.0.0/10",    # RFC 6598 CGNAT — used by some ingress fabrics
        "0.0.0.0/8",        # "this network"
        "::1/128",          # IPv6 loopback
        "fe80::/10",        # IPv6 link-local
        "fc00::/7",         # IPv6 unique-local
    )
)


def _is_internal(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _INTERNAL_NETWORKS if addr.version == net.version)


def _parse(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = value.strip()
    if not candidate:
        return None
    # IPv6 entries may legitimately arrive bracketed, optionally with a port.
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _forwarded_client(xff: str) -> str | None:
    """Rightmost externally-routable entry, or None when the header carries no usable address."""
    fallback: str | None = None
    for raw in reversed(xff.split(",")):
        parsed = _parse(raw)
        if parsed is None:
            continue
        if not _is_internal(parsed):
            return str(parsed)
        # Remember the rightmost valid-but-internal entry. A deployment that sits entirely
        # inside a private network legitimately has no external entry at all, and returning
        # None there would collapse every caller onto the proxy's own address.
        if fallback is None:
            fallback = str(parsed)
    return fallback


def client_ip(request: Any) -> str | None:
    """Resolve the originating client IP.

    ``X-Forwarded-For`` is honoured only when the deployment says it is behind a trusted
    ingress — either ``trust_forwarded_headers`` (managed ingress such as Azure Container
    Apps) or a direct peer listed in ``trusted_proxies``. Otherwise the header is ignored
    entirely and the socket peer is used, so a direct caller cannot claim to be someone else.
    """
    settings = get_settings()
    direct = request.client.host if request.client else None
    trusted = {ip.strip() for ip in (settings.trusted_proxies or "").split(",") if ip.strip()}
    if settings.trust_forwarded_headers or (direct and direct in trusted):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            resolved = _forwarded_client(xff)
            if resolved:
                return resolved
    return direct
