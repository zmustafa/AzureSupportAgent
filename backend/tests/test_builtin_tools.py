"""Tests for the built-in network utility tools (SSRF guards + wiring)."""
import asyncio

import app.agent.builtins as b


def test_metadata_and_private_ips_blocked():
    assert b._ip_is_blocked("169.254.169.254") is True  # cloud metadata
    assert b._ip_is_blocked("10.0.0.1") is True
    assert b._ip_is_blocked("172.16.5.5") is True
    assert b._ip_is_blocked("192.168.1.1") is True
    assert b._ip_is_blocked("127.0.0.1") is True
    assert b._ip_is_blocked("::1") is True


def test_public_ip_allowed():
    assert b._ip_is_blocked("8.8.8.8") is False
    assert b._ip_is_blocked("1.1.1.1") is False


def test_ipv6_ssrf_targets_blocked():
    # IPv6 must be guarded too: cloud-metadata, loopback, link-local, and unique-local.
    assert b._ip_is_blocked("fd00:ec2::254") is True  # AWS/Azure IMDS over IPv6
    assert b._ip_is_blocked("::1") is True  # loopback
    assert b._ip_is_blocked("fe80::1") is True  # link-local
    assert b._ip_is_blocked("fc00::1") is True  # unique-local (private)
    assert b._ip_is_blocked("fd12:3456::1") is True
    # A public IPv6 (Google DNS) is allowed.
    assert b._ip_is_blocked("2001:4860:4860::8888") is False


def test_unparseable_ip_is_blocked():
    # Anything that isn't a valid IP must fail closed (blocked), never allowed.
    assert b._ip_is_blocked("not-an-ip") is True
    assert b._ip_is_blocked("") is True



def test_hostname_validation_rejects_injection():
    ips, errmsg = b._resolve_safe_target("-x; rm -rf /")
    assert ips == []
    assert "not a valid hostname" in errmsg
    ips, errmsg = b._resolve_safe_target("a b c")
    assert ips == []


def test_resolve_blocks_loopback_name():
    ips, errmsg = b._resolve_safe_target("localhost")
    assert ips == []
    assert "private/blocked" in errmsg


def test_url_scheme_allowlist():
    host, errmsg = b._host_of_url("file:///etc/passwd")
    assert host == ""
    assert "http" in errmsg
    host, errmsg = b._host_of_url("ftp://example.com")
    assert host == ""
    host, errmsg = b._host_of_url("https://example.com/x")
    assert host == "example.com"
    assert errmsg is None


def test_port_check_validates_port():
    out = asyncio.run(b._port_check({}, {"host": "example.com", "port": 99999}))
    assert out["isError"] is True
    out = asyncio.run(b._port_check({}, {"host": "example.com"}))
    assert out["isError"] is True


def test_builtin_tools_all_read_only():
    for t in b.builtin_tool_catalog():
        assert t["kind"] == "read"
    names = {t["name"] for t in b.builtin_tool_catalog()}
    assert names == {
        "net_web_fetch", "net_http_request", "net_dns_lookup",
        "net_port_check", "net_ping", "net_traceroute",
        "azure_metrics",
    }


def test_kill_switch_and_filters(monkeypatch):
    # Disabled → no tools.
    monkeypatch.setattr(b, "_settings", lambda: {"builtin_tools_enabled": False})
    assert b.builtin_tools() == []
    # Enabled with one disabled.
    monkeypatch.setattr(b, "_settings", lambda: {"builtin_tools_enabled": True, "builtin_tools_disabled": ["net_traceroute"]})
    names = {t.name for t in b.builtin_tools()}
    assert "net_traceroute" not in names
    assert "net_ping" in names
    # Allow-list filter (custom agent scope).
    monkeypatch.setattr(b, "_settings", lambda: {"builtin_tools_enabled": True})
    only = b.builtin_tools(["net_dns_lookup"])
    assert [t.name for t in only] == ["net_dns_lookup"]


def test_egress_denylist(monkeypatch):
    monkeypatch.setattr(b, "_settings", lambda: {"network_egress_denylist": ["evil.com"]})
    assert b._egress_check("evil.com") is not None
    assert b._egress_check("sub.evil.com") is not None
    assert b._egress_check("good.com") is None


def test_egress_allowlist(monkeypatch):
    monkeypatch.setattr(b, "_settings", lambda: {"network_egress_allowlist": ["corp.com"]})
    assert b._egress_check("corp.com") is None
    assert b._egress_check("api.corp.com") is None
    assert b._egress_check("other.com") is not None
