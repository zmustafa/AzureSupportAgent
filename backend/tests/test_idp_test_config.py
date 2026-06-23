"""Tests for the best-effort OIDC/SAML provider config validation (the "Test connection"
button on the Sign-in & SSO admin screen)."""
from __future__ import annotations

import datetime as _dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.auth import oidc, saml


def _self_signed_cert(*, days_valid: int = 1) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _check(checks, name):
    return next((c for c in checks if c["name"] == name), None)


# ---------------------------------------------------------------- SAML
def test_saml_test_config_valid_cert():
    checks = saml.test_config({
        "entity_id": "https://idp.example/meta",
        "sso_url": "https://idp.example/sso",
        "certificate": _self_signed_cert(),
    })
    assert _check(checks, "IdP Entity ID set")["ok"] is True
    assert _check(checks, "IdP SSO URL set")["ok"] is True
    assert _check(checks, "Certificate parses")["ok"] is True
    assert _check(checks, "Certificate within validity period")["ok"] is True
    # All critical checks pass.
    assert all(c["ok"] for c in checks if c["critical"])


def test_saml_test_config_missing_and_bad_cert():
    checks = saml.test_config({"entity_id": "", "sso_url": "ftp://x", "certificate": "not-a-cert"})
    assert _check(checks, "IdP Entity ID set")["ok"] is False
    # ftp:// fails the (non-critical) URL check.
    assert _check(checks, "SSO URL is a valid URL")["ok"] is False
    assert _check(checks, "Certificate parses")["ok"] is False


# ---------------------------------------------------------------- OIDC
async def test_oidc_test_config_missing_fields():
    # No issuer/discovery + no client id → two failed required checks, returns early.
    checks = await oidc.test_config({})
    assert _check(checks, "Issuer / Discovery URL set")["ok"] is False
    assert _check(checks, "Client ID set")["ok"] is False


async def test_oidc_test_config_discovery(monkeypatch):
    async def fake_discover(issuer, discovery_url):
        return {
            "issuer": "https://idp.example/v2.0",
            "authorization_endpoint": "https://idp.example/authorize",
            "token_endpoint": "https://idp.example/token",
            "jwks_uri": "https://idp.example/keys",
        }

    monkeypatch.setattr(oidc, "discover", fake_discover)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": [{"kid": "k1"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda *a, **k: _Client())

    checks = await oidc.test_config({"issuer": "https://idp.example/v2.0", "client_id": "abc"})
    assert _check(checks, "Discovery document reachable")["ok"] is True
    assert _check(checks, "authorization_endpoint present")["ok"] is True
    assert _check(checks, "JWKS exposes signing keys")["ok"] is True
    assert all(c["ok"] for c in checks if c["critical"])


async def test_oidc_authorize_url_prompt(monkeypatch):
    async def fake_discover(issuer, discovery_url):
        return {"authorization_endpoint": "https://idp.example/authorize"}

    monkeypatch.setattr(oidc, "discover", fake_discover)

    # Default (no login_prompt) → no prompt param → IdP does silent SSO.
    url, _ = await oidc.build_authorize_url({"client_id": "c1"}, "https://app/cb")
    assert "prompt=" not in url

    # "Select account upon sign in" → prompt=select_account.
    url2, _ = await oidc.build_authorize_url({"client_id": "c1", "login_prompt": "select_account"}, "https://app/cb")
    assert "prompt=select_account" in url2
