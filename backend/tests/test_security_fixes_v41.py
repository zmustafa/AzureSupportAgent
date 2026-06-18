"""Tests for the v41 security hardening pass (audit follow-up):

- C1: SAML XML Signature Wrapping — identity must come from the SIGNED subtree only.
- H2: SAML audience / recipient / InResponseTo binding (replay + confusion defense).
- H1: SSO provisioning must not link a crafted assertion onto a local/password account.
- M1: ``must_change_password`` is enforced server-side (not just a client nudge).
- M4: cross-origin state-changing requests are rejected by the CSRF guard.

The SAML tests build REAL signed assertions (self-signed cert via ``cryptography`` +
``signxml``) so the wrapping attack is exercised end-to-end, exactly as a pen tester
would reproduce it.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt

import pytest
from lxml import etree

# --------------------------------------------------------------------------- helpers


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(scope="module")
def signing_material():
    """A self-signed RSA cert + key used to sign test SAML assertions."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


_ISSUER = "https://idp.example.com/metadata"


def _assertion_xml(
    *,
    aid: str,
    email: str,
    audience: str | None = None,
    recipient: str | None = None,
    in_response_to: str | None = None,
) -> str:
    now = _dt.datetime.utcnow()
    nb = (now - _dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    na = (now + _dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scd_attrs = [f'NotOnOrAfter="{na}"']
    if recipient:
        scd_attrs.append(f'Recipient="{recipient}"')
    if in_response_to:
        scd_attrs.append(f'InResponseTo="{in_response_to}"')
    aud_xml = (
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
        if audience
        else ""
    )
    return (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{aid}" Version="2.0" IssueInstant="{nb}">'
        f"<saml:Issuer>{_ISSUER}</saml:Issuer>"
        f"<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress">{email}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData {" ".join(scd_attrs)}/>'
        f"</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}">{aud_xml}</saml:Conditions>'
        f"<saml:AttributeStatement>"
        f'<saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>'
        f"</saml:AttributeStatement>"
        f"</saml:Assertion>"
    )


def _sign_assertion(assertion_xml: str, key_pem: str, cert_pem: str):
    from signxml import XMLSigner

    aid = etree.fromstring(assertion_xml.encode()).get("ID")
    # Exclusive C14N is the SAML standard — it excludes inherited ancestor namespaces, so
    # the signed assertion's digest stays valid when it's embedded inside a <Response>.
    return XMLSigner(c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#").sign(
        etree.fromstring(assertion_xml.encode()),
        key=key_pem,
        cert=cert_pem,
        reference_uri="#" + aid,
    )


def _wrap_in_response(*assertions) -> str:
    resp = etree.Element(
        "{urn:oasis:names:tc:SAML:2.0:protocol}Response",
        nsmap={"samlp": "urn:oasis:names:tc:SAML:2.0:protocol"},
    )
    for a in assertions:
        resp.append(a)
    return base64.b64encode(etree.tostring(resp)).decode()


# ------------------------------------------------------------------ C1: SAML XSW


def test_saml_happy_path_returns_signed_identity(signing_material):
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(aid="_good1", email="good@corp.com"), key_pem, cert_pem
    )
    b64 = _wrap_in_response(signed)
    identity = saml_mod.validate_response(b64, {"certificate": cert_pem, "entity_id": _ISSUER})
    assert identity["email"] == "good@corp.com"


def test_saml_signature_wrapping_uses_signed_assertion_not_injected(signing_material):
    """THE C1 attack: an unsigned attacker assertion injected as a sibling of the signed
    one must be ignored — identity comes from the verified (signed) subtree only."""
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(aid="_good2", email="good@corp.com"), key_pem, cert_pem
    )
    # Attacker injects an unsigned assertion claiming the admin identity, placed FIRST so
    # a naive `.find('.//Assertion')` would pick it (the old, vulnerable behavior).
    attacker = etree.fromstring(
        _assertion_xml(aid="_evil", email="admin@local").encode()
    )
    b64 = _wrap_in_response(attacker, signed)
    identity = saml_mod.validate_response(b64, {"certificate": cert_pem, "entity_id": _ISSUER})
    assert identity["email"] == "good@corp.com"
    assert identity["email"] != "admin@local"


def test_saml_tampered_assertion_fails_signature(signing_material):
    """Mutating the signed assertion after signing must fail verification."""
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(aid="_good3", email="good@corp.com"), key_pem, cert_pem
    )
    # Tamper: rewrite the NameID email inside the signed assertion.
    nid = signed.find(".//{urn:oasis:names:tc:SAML:2.0:assertion}NameID")
    nid.text = "attacker@corp.com"
    b64 = _wrap_in_response(signed)
    with pytest.raises(RuntimeError):
        saml_mod.validate_response(b64, {"certificate": cert_pem, "entity_id": _ISSUER})


def test_saml_wrong_cert_rejected(signing_material):
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(aid="_good4", email="good@corp.com"), key_pem, cert_pem
    )
    b64 = _wrap_in_response(signed)
    # A different self-signed cert must not validate the signature.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n2 = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other")])
    c2 = (
        x509.CertificateBuilder().subject_name(n2).issuer_name(n2)
        .public_key(k2.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=1))
        .sign(k2, hashes.SHA256())
    )
    other_cert = c2.public_bytes(serialization.Encoding.PEM).decode()
    with pytest.raises(RuntimeError):
        saml_mod.validate_response(b64, {"certificate": other_cert, "entity_id": _ISSUER})


# ------------------------------------------------------- H2: SAML binding checks


def test_saml_inresponseto_binding(signing_material):
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(
            aid="_b1", email="good@corp.com",
            audience="https://sp.example/meta", recipient="https://sp.example/acs",
            in_response_to="req-123",
        ),
        key_pem, cert_pem,
    )
    b64 = _wrap_in_response(signed)
    cfg = {"certificate": cert_pem, "entity_id": _ISSUER}
    # Matching request id ⇒ ok.
    ident = saml_mod.validate_response(
        b64, cfg, sp_entity_id="https://sp.example/meta",
        acs_url="https://sp.example/acs", expected_in_response_to="req-123",
    )
    assert ident["email"] == "good@corp.com"
    # Mismatched request id ⇒ rejected (replay / unsolicited).
    with pytest.raises(RuntimeError):
        saml_mod.validate_response(
            b64, cfg, sp_entity_id="https://sp.example/meta",
            acs_url="https://sp.example/acs", expected_in_response_to="DIFFERENT",
        )


def test_saml_audience_mismatch_rejected(signing_material):
    from app.auth import saml as saml_mod

    key_pem, cert_pem = signing_material
    signed = _sign_assertion(
        _assertion_xml(aid="_b2", email="good@corp.com", audience="https://sp.example/meta"),
        key_pem, cert_pem,
    )
    b64 = _wrap_in_response(signed)
    with pytest.raises(RuntimeError):
        saml_mod.validate_response(
            b64, {"certificate": cert_pem, "entity_id": _ISSUER},
            sp_entity_id="https://different-sp.example/meta",
        )


# ----------------------------------------------------- H1: SSO provisioning guard


async def _auth_engine(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    import app.models.auth  # noqa: F401
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_sso_cannot_take_over_local_account(tmp_path):
    from app.auth.passwords import hash_password
    from app.auth.provisioning import provision_sso_user
    from app.models.auth import IdentityProvider, User

    async def run():
        engine, Session = await _auth_engine(tmp_path)
        async with Session() as db:
            db.add(User(
                email="admin@local", username="admin", display_name="Admin",
                password_hash=hash_password("secret"), status="active",
                auth_source="local", tenant_id="default",
            ))
            db.add(IdentityProvider(id="idp1", name="Okta", type="saml", enabled=True, config_json={}))
            await db.commit()
            idp = await db.get(IdentityProvider, "idp1")
            # Crafted assertion claiming the local admin's email ⇒ MUST be refused.
            taken = await provision_sso_user(
                db, idp, external_id="evil-sub", email="admin@local",
                display_name="Evil", groups=[], email_verified=True,
            )
            assert taken is None
        await engine.dispose()

    _run(run())


def test_sso_links_existing_sso_account_by_email(tmp_path):
    from app.auth.provisioning import provision_sso_user
    from app.models.auth import IdentityProvider, User

    async def run():
        engine, Session = await _auth_engine(tmp_path)
        async with Session() as db:
            db.add(IdentityProvider(id="idp1", name="Okta", type="saml", enabled=True, config_json={}))
            db.add(User(
                email="bob@corp.com", username="bob", display_name="Bob",
                password_hash=None, status="active", auth_source="saml",
                external_idp="idp1", external_id="bob-old", tenant_id="default",
            ))
            await db.commit()
            idp = await db.get(IdentityProvider, "idp1")
            # New subject, same (SSO-managed, passwordless) email ⇒ allowed to link.
            linked = await provision_sso_user(
                db, idp, external_id="bob-new", email="bob@corp.com",
                display_name="Bob", groups=[], email_verified=True,
            )
            assert linked is not None and linked.username == "bob"
            # Unverified email must NOT link.
            db.add(User(
                email="dave@corp.com", username="dave", display_name="Dave",
                password_hash=None, status="active", auth_source="saml",
                external_idp="idp1", external_id="dave-old", tenant_id="default",
            ))
            await db.commit()
            blocked = await provision_sso_user(
                db, idp, external_id="dave-new", email="dave@corp.com",
                display_name="Dave", groups=[], email_verified=False,
            )
            assert blocked is None
        await engine.dispose()

    _run(run())


# --------------------------------------------------- M1: forced password change


def test_must_change_password_enforced_server_side(tmp_path):
    from datetime import datetime, timedelta, timezone

    from starlette.requests import Request

    from app.core.security import get_principal
    from app.models.auth import Session as AuthSession
    from app.models.auth import User

    sid = "sess-mcp-1"

    async def run():
        engine, Session = await _auth_engine(tmp_path)
        async with Session() as db:
            u = User(
                id="u1", email="u1@corp.com", username="u1", display_name="U1",
                password_hash="x", status="active", auth_source="local",
                must_change_password=True, tenant_id="default",
            )
            db.add(u)
            now = datetime.now(timezone.utc)
            db.add(AuthSession(
                id=sid, user_id="u1", created_at=now, last_seen_at=now,
                expires_at=now + timedelta(days=1),
            ))
            await db.commit()

        def _req(path: str) -> Request:
            return Request({
                "type": "http", "method": "GET", "path": path,
                "headers": [], "query_string": b"", "scheme": "http",
                "server": ("testserver", 80),
            })

        async with Session() as db:
            from fastapi import HTTPException

            # Allowlisted identity endpoint is reachable.
            p = await get_principal(_req("/api/me"), azsupagent_session=sid, db=db)
            assert p.must_change_password is True
            # A protected endpoint is blocked with 403 until the password is changed.
            with pytest.raises(HTTPException) as ei:
                await get_principal(_req("/api/admin/access/users"), azsupagent_session=sid, db=db)
            assert ei.value.status_code == 403
        await engine.dispose()

    _run(run())


# ----------------------------------------------------------- M4: CSRF guard


def test_csrf_origin_enforcement():
    """Cross-origin writes are blocked; same-origin writes pass the guard.

    The guard is mounted on an ISOLATED minimal app (not the real one) so this test
    doesn't spin up the full lifespan — which would re-bind the monitor sampler's
    module-level asyncio.Event to a second event loop and crash."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import _CsrfGuard

    origin = get_settings().frontend_origin
    test_app = FastAPI()
    test_app.add_middleware(_CsrfGuard)

    @test_app.post("/api/echo")
    async def _echo():  # pragma: no cover - exercised via TestClient
        return {"ok": True}

    @test_app.post("/api/auth/saml/x/acs")
    async def _acs():  # pragma: no cover - exercised via TestClient
        return {"acs": True}

    with TestClient(test_app) as client:
        # Foreign Origin on a state-changing request ⇒ blocked before the handler.
        blocked = client.post("/api/echo", headers={"Origin": "https://evil.example"})
        assert blocked.status_code == 403
        assert "cross-origin" in blocked.json().get("detail", "").lower()

        # Same-origin request passes the guard.
        allowed = client.post("/api/echo", headers={"Origin": origin})
        assert allowed.status_code == 200

        # No Origin (non-browser client / curl) is allowed — no ambient cookie to abuse.
        no_origin = client.post("/api/echo")
        assert no_origin.status_code == 200

        # The SAML ACS is exempt (IdP-posted cross-site form, protected by the signed
        # assertion + single-use InResponseTo cookie instead).
        acs = client.post("/api/auth/saml/x/acs", headers={"Origin": "https://idp.example"})
        assert acs.status_code == 200

        # A cross-site fetch flagged by Sec-Fetch-Site (no Origin) is blocked too.
        sec_fetch = client.post("/api/echo", headers={"Sec-Fetch-Site": "cross-site"})
        assert sec_fetch.status_code == 403
