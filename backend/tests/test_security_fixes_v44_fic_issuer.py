"""FIC issuer trust was decided by url substring, not by host.

`app/entra/collectors/apps.py::fic_trusted` classified a federated identity credential's
issuer as trusted using:

    any(iss.startswith(p.lower()) for p in _TRUSTED_FIC_PREFIXES) or ".oic.prod-aks.azure.com" in iss

Two independent bypasses:

1. Seven of the nine prefixes had no trailing "/", so `https://gitlab.com.evil.com/`
   satisfies `startswith("https://gitlab.com")`. `.evil.com` is registrable.
2. The AKS check was an `in` test against the WHOLE string, so the substring matching
   anywhere -- including the path -- was enough: `https://evil.com/.oic.prod-aks.azure.com/x`.

The issuer is attacker-supplied in the threat model this collector exists to serve: it is
whatever was configured on the app registration's federated credential. A "trusted" flag
that an attacker can set at will is worse than no flag, because the report actively
reassures the reader. CodeQL `py/incomplete-url-substring-sanitization`.
"""
from __future__ import annotations

import pytest

from app.entra.collectors.apps import fic_trusted


@pytest.mark.parametrize(
    "issuer",
    [
        "https://token.actions.githubusercontent.com",
        "https://token.actions.githubusercontent.com/",
        "https://vstoken.dev.azure.com/abc",
        "https://login.microsoftonline.com/tenant-id/v2.0",
        "https://sts.windows.net/00000000-0000-0000-0000-000000000000/",
        "https://gitlab.com",
        "https://gitlab.com/group/project",
        "https://oidc.prod-aks.azure.com/abc",
        "https://kubernetes.default.svc",
        "https://container.googleapis.com/v1/projects/p/locations/l/clusters/c",
        "https://token.actions.github.com",
        "https://eastus.oic.prod-aks.azure.com/tenant/guid/",  # regional AKS issuer
        "HTTPS://TOKEN.ACTIONS.GITHUBUSERCONTENT.COM",  # case must not matter
        "  https://gitlab.com  ",  # surrounding whitespace was tolerated before
    ],
)
def test_real_issuers_are_still_trusted(issuer):
    assert fic_trusted(issuer) is True, f"regressed a legitimate issuer: {issuer!r}"


@pytest.mark.parametrize(
    "issuer",
    [
        # Bypass 1: suffix on a prefix that lacked a trailing delimiter.
        "https://gitlab.com.evil.com/",
        "https://token.actions.githubusercontent.com.evil.com/x",
        "https://token.actions.github.com.evil.com/",
        "https://vstoken.dev.azure.com.evil.com/",
        "https://oidc.prod-aks.azure.com.evil.com/",
        "https://kubernetes.default.svc.evil.com/",
        "https://container.googleapis.com.evil.com/",
        # Bypass 2: the AKS substring appearing in the PATH.
        "https://evil.com/.oic.prod-aks.azure.com/x",
        "https://evil.com/?x=.oic.prod-aks.azure.com",
        "https://evil.com/#.oic.prod-aks.azure.com",
        # The leading dot on the suffix is what stops this one.
        "https://evil-oic.prod-aks.azure.com/",
        # Userinfo: the trusted name appears before the '@', so the host is evil.com.
        "https://token.actions.githubusercontent.com@evil.com/",
        # Plain wrong, and non-https.
        "https://evil.com/",
        "http://gitlab.com/",
        "gitlab.com",
        "",
        "   ",
        "not a url",
    ],
)
def test_lookalike_and_path_embedded_issuers_are_not_trusted(issuer):
    assert fic_trusted(issuer) is False, f"classified an untrusted issuer as trusted: {issuer!r}"


# ---------------------------------------------------------------------- non-vacuity
_OLD_PREFIXES = (
    "https://token.actions.githubusercontent.com",
    "https://vstoken.dev.azure.com",
    "https://login.microsoftonline.com/",
    "https://sts.windows.net/",
    "https://gitlab.com",
    "https://oidc.prod-aks.azure.com",
    "https://kubernetes.default.svc",
    "https://container.googleapis.com",
    "https://token.actions.github.com",
)


def _fic_trusted_old(issuer: str) -> bool:
    """The previous implementation, verbatim."""
    iss = (issuer or "").strip().lower()
    return any(iss.startswith(p.lower()) for p in _OLD_PREFIXES) or ".oic.prod-aks.azure.com" in iss


@pytest.mark.parametrize(
    "issuer",
    [
        "https://gitlab.com.evil.com/",
        "https://token.actions.githubusercontent.com.evil.com/x",
        "https://evil.com/.oic.prod-aks.azure.com/x",
    ],
)
def test_the_old_check_really_did_trust_these(issuer):
    """Proves the cases above are testing a defect that existed, not a hypothetical.

    `https://evil-oic.prod-aks.azure.com/` is deliberately NOT in this list: the old check
    already rejected it (it requires a leading dot), so it is a hardening case rather than
    a demonstrated bypass, and claiming otherwise would be false.
    """
    assert _fic_trusted_old(issuer) is True, (
        f"{issuer!r} was not accepted by the old check -- this regression test no longer "
        "demonstrates the bug it was written for"
    )
