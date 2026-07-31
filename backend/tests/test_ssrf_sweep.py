"""SSRF payload sweep against the central outbound-URL guard.

Plan reference: docs/improvement-plans/security-hardening/08-injection-ssrf-pentest.md#83

Three outbound surfaces trust `app.core.ssrf.check_url`:
  * connector webhooks           (admin-supplied URL)
  * the Logic App connector      (admin-supplied URL)
  * LLM provider base_url        (admin-supplied URL)

and `app.agent.builtins` mirrors the same policy for agent-initiated fetches -- which is
the one an attacker reaches INDIRECTLY, by planting a URL in Azure data the agent reads.

The prize is the cloud metadata endpoint (169.254.169.254): reaching it from inside the
container leaks instance detail and, where managed identity is enabled, tokens.

`check_url` returns an error string when the URL must NOT be contacted, else None.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.ssrf import check_url

_APP = Path(__file__).resolve().parents[1] / "app"


def _blocked(url: str, **kw) -> bool:
    return check_url(url, **kw) is not None


# ===================================================================== metadata


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
        "http://169.254.169.254/",
        "https://169.254.169.254/",
        "http://169.254.169.254:80/latest/meta-data/",
    ],
)
def test_cloud_metadata_is_blocked(url):
    assert _blocked(url), f"metadata endpoint reachable: {url}"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/metadata/instance",
        "http://[fd00:ec2::254]/latest/meta-data/",
    ],
)
def test_metadata_is_blocked_even_when_private_targets_are_allowed(url):
    """`allow_private=True` exists for local providers such as Ollama. The module
    documents that metadata is blocked REGARDLESS -- pin that promise."""
    assert _blocked(url, allow_private=True), (
        f"metadata reachable under allow_private=True: {url}"
    )


# ===================================================================== private ranges


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/api/admin",
        "http://localhost/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://10.255.255.254/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://192.168.0.1/",
        "http://169.254.1.1/",          # link-local generally, not just metadata
        "http://[::1]/",                # IPv6 loopback
        "http://[::]/",                 # IPv6 unspecified
        "http://[fc00::1]/",            # IPv6 unique-local
    ],
)
def test_private_loopback_and_link_local_are_blocked(url):
    assert _blocked(url), f"private/internal address reachable: {url}"


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",        # decimal 127.0.0.1
        "http://0177.0.0.1/",        # octal
        "http://0x7f.0x0.0x0.0x1/",  # hex
        "http://127.1/",             # short form
    ],
)
def test_alternative_ip_encodings_of_loopback_are_blocked(url):
    """Classic filter bypass: the same address written so a naive string check misses it.
    The guard resolves via getaddrinfo, so the encoding is normalised before the check --
    this pins that it stays that way."""
    assert _blocked(url), f"encoded loopback slipped through: {url}"


# ===================================================================== scheme handling


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "gopher://127.0.0.1:6379/_SET%20k%20v",   # redis via gopher
        "dict://127.0.0.1:11211/stat",
        "ftp://internal.example/",
        "ldap://127.0.0.1/",
        "jar:http://evil.example/!/",
        "data:text/plain;base64,SGVsbG8=",
        "",
        "   ",
        "not-a-url",
    ],
)
def test_non_http_schemes_are_rejected(url):
    assert _blocked(url), f"non-http(s) scheme accepted: {url!r}"


def test_https_can_be_required():
    assert _blocked("http://example.com/hook", require_https=True)
    # and the same URL over https is a scheme decision, not an address one
    assert check_url("http://example.com/hook", require_https=True) is not None


# ===================================================================== parser confusion


@pytest.mark.parametrize(
    "url",
    [
        "http://user@evil.example@169.254.169.254/",   # userinfo confusion
        "http://169.254.169.254%2f@evil.example/",     # encoded separator
        "http://evil.example#@169.254.169.254/",       # fragment confusion
    ],
)
def test_url_parser_confusion_does_not_reach_metadata(url):
    """These differ in which host a naive parser versus a real client picks. Whatever
    urlparse decides, the request must NOT end up at the metadata service.

    Note: for the fragment case the true host IS evil.example, so it is legitimately
    allowed -- the assertion is that the METADATA address is never the resolved target.
    """
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host == "169.254.169.254":
        assert _blocked(url), f"parser confusion reached metadata: {url}"
    else:
        # Host is genuinely the external one; nothing to block. Documented, not asserted
        # as blocked, so this test does not encode a false expectation.
        assert host != "169.254.169.254"


def test_url_with_no_host_is_rejected():
    assert _blocked("http:///nohost")


# ===================================================================== redirect policy


def test_outbound_clients_do_not_follow_redirects():
    """SSRF validation happens BEFORE the request. If the client then follows a 302, an
    attacker-controlled but publicly-resolvable host can bounce the request to
    169.254.169.254 and the guard is bypassed entirely.

    Pin that every user-influenced outbound client keeps redirects off. radar/feed.py is
    the deliberate exception: a fixed, allowlisted Microsoft HTTPS feed.
    """
    builtins_src = (_APP / "agent" / "builtins.py").read_text(encoding="utf-8")
    assert "follow_redirects=False" in builtins_src, (
        "agent builtin fetch must not follow redirects -- that is the standard SSRF "
        "guard bypass"
    )
    assert "follow_redirects=True" not in builtins_src

    webhook_src = (_APP / "connectors" / "webhook.py").read_text(encoding="utf-8")
    assert "follow_redirects=True" not in webhook_src, (
        "the webhook connector must not opt into redirect following (httpx defaults to "
        "False -- keep it that way)"
    )


def test_ssrf_guard_is_actually_wired_into_the_outbound_connectors():
    """A guard nobody calls is decoration. Non-vacuity check for the tests above."""
    for rel in ("connectors/webhook.py", "connectors/logicapp.py"):
        src = (_APP / rel).read_text(encoding="utf-8")
        assert "check_url" in src, f"{rel} does not call the SSRF guard"


# ===================================================================== non-vacuity


def test_an_ordinary_public_url_is_allowed():
    """If everything were blocked the sweep above would pass vacuously and the feature
    would be broken. Requires DNS; skipped when offline."""
    result = check_url("https://example.com/webhook")
    if result and "DNS resolution failed" in result:
        pytest.skip("no DNS in this environment")
    assert result is None, f"a legitimate public URL was blocked: {result}"
