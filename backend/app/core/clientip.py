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

# Ranges that can only be infrastructure, never an originating client. Used to skip proxy hops
# while walking the forwarded header from the right.
#
# Deliberately NOT expressed as `not ip.is_global`: Python classifies the RFC 5737 / RFC 3849
# DOCUMENTATION ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) as
# private, so `is_global` would discard exactly the addresses every test fixture and worked
# example uses. This list names the internal ranges explicitly instead.
#
# 100.64.0.0/10 (RFC 6598 CGNAT) is DELIBERATELY ABSENT, and that absence was bought the hard
# way. It was originally listed as "used by some ingress fabrics", which was too confident:
# CGNAT space is a legitimate CLIENT identity in at least two common deployments —
#   * a tailnet address (Tailscale allocates from 100.64.0.0/10), which is exactly the stable,
#     per-device identity an operator would want to allowlist; and
#   * any ISP applying carrier-grade NAT toward its subscribers.
# Skipping it silently discarded the real caller and attributed the request to whatever came
# next in the chain. Treat CGNAT as a client; if a specific deployment really does have a CGNAT
# proxy hop, list it in `trusted_proxies` rather than blanket-skipping a /10.
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",       # RFC 1918
        "172.16.0.0/12",    # RFC 1918
        "192.168.0.0/16",   # RFC 1918
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local
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
    """Rightmost externally-attributable entry, or None when there is no usable address.

    Returns None rather than falling back to a proxy address. An earlier version returned the
    rightmost valid-but-internal entry when nothing else matched, which meant an infrastructure
    hop could be handed back as "the client" — and therefore be matched against an allowlist.
    Refusing to name a caller we cannot attribute is the correct direction for a security
    control: the allowlist treats None as "no match", so such a request is refused rather than
    quietly admitted under a proxy's identity.
    """
    for raw in reversed(xff.split(",")):
        parsed = _parse(raw)
        if parsed is None:
            continue
        if not _is_internal(parsed):
            return str(parsed)
    return None


def describe(request: Any) -> dict[str, Any]:
    """Explain how the caller's address was resolved, for the admin diagnostic.

    "Which address does the server think I am, and how did it decide?" is the first question
    anyone debugging an allowlist asks. Without this it takes a screenshot and a conversation
    to answer, which is how a mis-attributed CGNAT address went unnoticed.
    """
    settings = get_settings()
    direct = request.client.host if request.client else None
    trusted = {ip.strip() for ip in (settings.trusted_proxies or "").split(",") if ip.strip()}
    header = request.headers.get("x-forwarded-for")
    honoured = bool(settings.trust_forwarded_headers or (direct and direct in trusted))

    entries: list[dict[str, Any]] = []
    if header:
        for raw in header.split(","):
            parsed = _parse(raw)
            entries.append(
                {
                    "value": raw.strip(),
                    "valid": parsed is not None,
                    "classification": (
                        "unparseable"
                        if parsed is None
                        else ("infrastructure" if _is_internal(parsed) else "client")
                    ),
                }
            )

    resolved = client_ip(request)
    for entry in entries:
        entry["selected"] = entry["valid"] and entry["value"] == resolved

    if not header:
        reason = "No X-Forwarded-For header; using the socket peer."
    elif not honoured:
        reason = (
            "X-Forwarded-For was ignored because this deployment does not trust it "
            "(TRUST_FORWARDED_HEADERS is off and the peer is not a configured trusted proxy); "
            "using the socket peer."
        )
    elif resolved is None:
        reason = (
            "X-Forwarded-For contained no address attributable to a client — every entry was "
            "infrastructure or unparseable. The caller cannot be identified, so it is refused."
        )
    else:
        reason = (
            "Read right-to-left; took the rightmost entry that is not infrastructure, because "
            "a caller can only prepend to this header."
        )

    return {
        "resolved_ip": resolved,
        # NOTE: this is the peer as reported by the ASGI server, NOT necessarily the raw TCP
        # peer. uvicorn enables proxy-header handling by default and rewrites scope["client"]
        # from X-Forwarded-For for allowed peers, so this field can itself be header-derived.
        # Our own resolution never uses it when a trusted header is present, but a diagnostic
        # that quietly presented it as ground truth would be its own small lie.
        "socket_peer": direct,
        "forwarded_header": header,
        "forwarded_honoured": honoured,
        "entries": entries,
        "reason": reason,
    }



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
            # The header is present AND trusted, so it — not the socket peer — is the authority
            # on who is calling. If it yields nothing attributable, return None rather than
            # falling back to `direct`: behind a trusted proxy `direct` IS the proxy, and
            # handing that back would let infrastructure satisfy an allowlist.
            return _forwarded_client(xff)
    return direct
