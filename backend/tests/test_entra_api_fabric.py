"""The authentication perimeter, as it reaches the browser.

Three screens quote the federation state and each of them draws a different conclusion
from it: the setup checklist explains the perimeter, the posture header qualifies the
score, and the auth-methods tab warns that its MFA registration numbers describe only the
half of the directory Entra actually authenticates. If the key silently stops arriving,
none of those screens breaks — they just quietly go back to overstating their own numbers.
So the presence and the shape are pinned here.

The last test is the one that matters most: a signing certificate is public material, but
shipping it to every browser that opens a tab is still a needless widening of what this
product holds. Only the derived facts travel.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.api import entra as entra_api
from app.entra import cache, demo, federation
from app.entra import snapshot as snapshot_mod


class _Principal:
    tenant_id = demo.DEMO_TENANT
    subject = "dev"


def _self_signed(days_valid: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pf.contoso.com")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


CERT_B64 = _self_signed(120)


@pytest.fixture(autouse=True)
def _federated_demo(tmp_path, monkeypatch):
    """The demo tenant, re-seeded with a federated domain.

    The trust is built by running a realistic Graph body through the real normaliser rather
    than hand-writing the stored shape, so the redaction test below exercises the actual
    collector path instead of a convenient stand-in.
    """
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation

    import app.core.azure_connections as ac

    monkeypatch.setattr(
        ac, "resolve_connection",
        lambda cid: {"id": "conn-demo", "tenant_id": demo.DEMO_TENANT} if cid == "conn-demo" else None,
    )
    demo.seed()

    trust = federation.normalize_federation("contoso.com", {
        "displayName": "PingFederate",
        "issuerUri": "http://contoso.com/PingFederate",
        "passiveSignInUri": "https://pf.contoso.com/idp/a/prp.wsf",
        "preferredAuthenticationProtocol": "wsFed",
        "federatedIdpMfaBehavior": "acceptIfMfaDoneByFederatedIdp",
        "signingCertificate": CERT_B64,
        "nextSigningCertificate": "",
        "signingCertificateUpdateStatus": {"certificateUpdateResult": "Success", "lastRunDateTime": "2026-01-01T00:00:00Z"},
    })
    trust["user_count"], trust["user_share"] = 12, 0.6

    payload = demo._tenant()  # noqa: SLF001 - the demo module is the fixture
    payload["data"]["identity_fabric"] = {
        "readable": True, "blind_reason": "",
        "domains": [
            {"name": "contoso.com", "authentication_type": "Federated", "federated": True,
             "is_default": True, "is_initial": False, "is_verified": True},
            {"name": "contoso.onmicrosoft.com", "authentication_type": "Managed", "federated": False,
             "is_default": False, "is_initial": True, "is_verified": True},
        ],
        "federation": [trust],
        "federated_count": 1, "managed_count": 1,
        "external_idps": [], "external_idps_readable": False,
        "external_idps_reason": "IdentityProvider.Read.All",
    }
    cache.write_domain(demo.DEMO_TENANT, "tenant", payload)
    snapshot_mod.invalidate(demo.DEMO_TENANT)
    yield
    cache.clear_memo()


def _run(coro):
    return asyncio.run(coro)


def _setup():
    return _run(entra_api.setup_checklist(connection_id="conn-demo", principal=_Principal()))


def _posture():
    return _run(entra_api.posture(connection_id="conn-demo", principal=_Principal()))


def _auth_methods():
    return _run(entra_api.signals_auth_methods(connection_id="conn-demo", principal=_Principal()))


def _users() -> list[dict]:
    snap = snapshot_mod.analyze(demo.DEMO_TENANT)
    return ((snap.get("data") or {}).get("people") or {}).get("users") or []


def _expected_population() -> int:
    return sum(1 for u in _users() if str(u.get("upn") or "").lower().endswith("@contoso.com"))


def _expected_share() -> float:
    return _expected_population() / max(len(_users()), 1)


# ------------------------------------------------------------------------ the full block
def test_the_setup_checklist_carries_the_whole_perimeter():
    fabric = _setup()["identity_fabric"]
    assert fabric["readable"] is True
    assert fabric["federated"] is True
    assert [d["name"] for d in fabric["domains"]] == ["contoso.com", "contoso.onmicrosoft.com"]
    assert fabric["federated_count"] == 1 and fabric["managed_count"] == 1
    trust = fabric["federation"][0]
    assert trust["vendor"]["label"] == "PingFederate"
    assert trust["host"] == "pf.contoso.com"
    assert trust["certificate"]["parsed"] is True


def test_the_summary_names_the_provider_and_the_population():
    """The one line the header renders. Numbers without a provider name explain nothing."""
    summary = _setup()["identity_fabric"]["summary"]
    assert "PingFederate" in summary
    assert f"{round(_expected_share() * 100)}% of users" in summary


def test_a_cloud_only_tenant_says_so_rather_than_going_quiet():
    fabric = entra_api._identity_fabric({"data": {"tenant": {  # noqa: SLF001
        "identity_fabric": {"readable": True, "domains": [{"name": "a.com"}], "federation": []},
    }}})
    assert fabric["federated"] is False
    assert "No external provider is federated" in fabric["summary"]


def test_an_unreadable_perimeter_produces_no_summary_at_all():
    """Blind must not render as "all clean" — an empty summary makes the card say so."""
    fabric = entra_api._identity_fabric({"data": {"tenant": {  # noqa: SLF001
        "identity_fabric": {"readable": False, "blind_reason": "Domain.Read.All"},
    }}})
    assert fabric["summary"] == ""
    assert fabric["blind_reason"] == "Domain.Read.All"


# ----------------------------------------------------------------------------- the brief
@pytest.mark.parametrize("call", [_posture, _auth_methods])
def test_the_qualifying_screens_carry_the_brief(call):
    brief = call()["identity_fabric"]
    assert brief["federated"] is True
    assert brief["vendors"] == ["PingFederate"]
    assert brief["domains"] == ["contoso.com"]
    # Counted from the directory, not from whatever the collector last stored: the seeded
    # figures above are deliberately wrong so this asserts the join, not the echo.
    assert brief["user_count"] == _expected_population()
    assert brief["user_share"] == pytest.approx(_expected_share(), abs=1e-4)


@pytest.mark.parametrize("call", [_posture, _auth_methods])
def test_the_brief_stays_brief(call):
    """Endpoints and certificate facts belong on the setup screen and nowhere else."""
    brief = call()["identity_fabric"]
    assert set(brief) == {
        "readable", "federated", "federated_count", "managed_count", "vendors", "domains",
        "user_count", "user_share", "sync_enabled", "password_sync", "summary",
    }


# ------------------------------------------------------------------------------ redaction
@pytest.mark.parametrize("call", [_setup, _posture, _auth_methods])
def test_no_payload_ever_carries_the_signing_certificate(call):
    body = str(call())
    assert CERT_B64 not in body
    assert "signingCertificate" not in body
    assert "BEGIN CERTIFICATE" not in body
