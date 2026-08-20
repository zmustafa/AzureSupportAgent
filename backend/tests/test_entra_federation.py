"""The authentication perimeter: federation fingerprinting, certificates, and signals.

The case these tests were written around: a signing certificate that expired weeks ago with
a valid successor still carrying the trust. A first cut declared that an outage. It was not
— Entra accepts either certificate, and thousands of people were signing in perfectly well.
Several tests below exist purely to keep that mistake from coming back.

Fixtures use synthetic domains and providers only.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.entra import federation as fed
from app.entra.signal_defs import fed as fed_signals
from app.entra.signals import SignalContext, SignalUnavailable


# --------------------------------------------------------------------------- vendors
@pytest.mark.parametrize("issuer,endpoint,expected", [
    ("http://contoso.com/PingFederate", "https://pf.example.com/idp/x/prp.wsf", "PingFederate"),
    ("urn:contoso", "https://sso.pingone.com/idp", "PingOne"),
    ("http://contoso.com/adfs/services/trust", "https://sts.contoso.com/adfs/ls/", "Active Directory Federation Services"),
    ("urn:acme", "https://acme.okta.com/app/x/sso/saml", "Okta"),
    ("urn:acme", "https://acme.onelogin.com/trust/saml2", "OneLogin"),
    ("urn:acme", "https://acme.auth0.com/samlp", "Auth0"),
    ("https://accounts.google.com/o/saml2", "", "Google Workspace"),
])
def test_known_providers_are_named(issuer, endpoint, expected):
    assert fed.fingerprint_vendor(issuer, endpoint)["label"] == expected


def test_an_unknown_provider_is_reported_as_unknown_rather_than_guessed():
    """A confident wrong answer about the authentication perimeter is worse than no answer."""
    got = fed.fingerprint_vendor("urn:internal:sso", "https://login.internal.example/saml")
    assert got["key"] == fed.UNKNOWN_VENDOR
    assert got["label"] == "Unrecognized provider"


def test_the_host_survives_even_when_only_one_endpoint_is_populated():
    assert fed.endpoint_host("", "", "https://pf.example.com/idp/a/sts.wst") == "pf.example.com"
    assert fed.endpoint_host("", "") == ""


# ---------------------------------------------------------------------- certificates
def _self_signed(days_valid: int, *, cn: str = "pf.example.com") -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=365))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def test_certificate_facts_are_derived_and_the_key_material_is_not_returned():
    facts = fed.certificate_facts(_self_signed(90))
    assert facts["parsed"] is True
    assert facts["thumbprint"] and len(facts["thumbprint"]) == 40
    assert 88 <= facts["days_left"] <= 90
    assert facts["expired"] is False
    # The certificate itself must never travel with its own facts.
    assert not any("BEGIN CERTIFICATE" in str(v) for v in facts.values())
    assert "certificate" not in facts and "raw" not in facts


def test_an_expired_certificate_reports_negative_days():
    facts = fed.certificate_facts(_self_signed(-10))
    assert facts["expired"] is True
    assert facts["days_left"] < 0


def test_a_malformed_certificate_is_reported_rather_than_raised():
    """One bad certificate must not take the whole tenant collection down."""
    assert fed.certificate_facts("not base64 at all")["parsed"] is False
    assert fed.certificate_facts(base64.b64encode(b"nonsense").decode())["parsed"] is False
    assert fed.certificate_facts("") == {}


# ------------------------------------------------------------------------ behaviors
def test_an_unset_mfa_behaviour_is_reported_as_the_permissive_default():
    """Empty means Entra applies acceptIfMfaDoneByFederatedIdp, not "not configured"."""
    behaviour = fed.mfa_behaviour("")
    assert behaviour["value"] == "acceptIfMfaDoneByFederatedIdp"
    assert behaviour["explicit"] is False
    assert behaviour["trusted"] is True


def test_enforced_and_rejected_behaviours_are_not_trusted():
    assert fed.mfa_behaviour("enforceMfaByFederatedIdp")["trusted"] is False
    assert fed.mfa_behaviour("rejectMfaByFederatedIdp")["trusted"] is False


# -------------------------------------------------------------------------- fixtures
def _ctx() -> SignalContext:
    return SignalContext(tenant_id="t")


def _data(*, trusts=None, readable=True, hybrid=None, blind_reason=""):
    return {
        "tenant": {
            "identity_fabric": {
                "readable": readable,
                "blind_reason": blind_reason,
                "domains": [{"name": "contoso.com", "federated": bool(trusts)}],
                "federation": list(trusts or []),
                "federated_count": len(trusts or []),
                "managed_count": 1,
            },
            "hybrid": hybrid if hybrid is not None else {"features_readable": True, "sync_enabled": False},
        }
    }


def _trust(**over):
    base = {
        "domain": "contoso.com",
        "vendor": {"key": "pingfederate", "label": "PingFederate"},
        "issuer_uri": "http://contoso.com/PingFederate",
        "protocol": "wsFed",
        "mfa_behaviour": fed.mfa_behaviour(""),
        "certificate": fed.certificate_facts(_self_signed(200)),
        "next_certificate": {},
        "auto_rollover": {"result": "Success", "last_run": "", "healthy": True},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------- signals
def test_a_trusted_mfa_claim_is_a_finding():
    out = fed_signals._federated_mfa_trusted(_data(trusts=[_trust()]), _ctx())
    assert len(out) == 1
    assert "PingFederate" in out[0]["title"]
    assert out[0]["evidence"]["explicitly_set"] is False


def test_an_enforced_mfa_claim_is_not_a_finding():
    trust = _trust(mfa_behaviour=fed.mfa_behaviour("enforceMfaByFederatedIdp"))
    assert fed_signals._federated_mfa_trusted(_data(trusts=[trust]), _ctx()) == []


def test_a_healthy_certificate_raises_nothing():
    assert fed_signals._federation_cert_expiry(_data(trusts=[_trust()]), _ctx()) == []


@pytest.mark.parametrize("days,severity", [(5, "critical"), (20, "high"), (45, "medium")])
def test_certificate_expiry_severity_tracks_the_days_remaining(days, severity):
    trust = _trust(certificate=fed.certificate_facts(_self_signed(days)))
    out = fed_signals._federation_cert_expiry(_data(trusts=[trust]), _ctx())
    assert len(out) == 1 and out[0]["severity"] == severity


def test_an_expired_primary_with_a_valid_successor_is_not_an_outage():
    """The live tenant's exact state, and the bug this test exists to prevent.

    Entra accepts either certificate. Judging the trust by the primary alone reported a
    tenant-wide outage on a tenant whose users were signing in all day.
    """
    trust = _trust(
        certificate=fed.certificate_facts(_self_signed(-74)),
        next_certificate=fed.certificate_facts(_self_signed(124)),
    )
    assert fed_signals._federation_cert_expiry(_data(trusts=[trust]), _ctx()) == []
    stale = fed_signals._federation_cert_stale_primary(_data(trusts=[trust]), _ctx())
    assert len(stale) == 1 and stale[0]["severity"] == "low"


def test_both_certificates_expired_is_an_outage():
    trust = _trust(
        certificate=fed.certificate_facts(_self_signed(-74)),
        next_certificate=fed.certificate_facts(_self_signed(-1)),
    )
    out = fed_signals._federation_cert_expiry(_data(trusts=[trust]), _ctx())
    assert len(out) == 1 and out[0]["severity"] == "critical"
    assert out[0]["evidence"]["primary_expired"] is True


def test_a_completed_rollover_is_not_a_finding_but_notfound_is():
    assert fed_signals._federation_cert_rollover(_data(trusts=[_trust()]), _ctx()) == []
    trust = _trust(auto_rollover={"result": "NotFound", "last_run": "2026-05-18", "healthy": False})
    out = fed_signals._federation_cert_rollover(_data(trusts=[trust]), _ctx())
    assert len(out) == 1 and "NotFound" in out[0]["title"]


def test_password_hash_sync_is_only_a_finding_when_the_tenant_is_federated():
    hybrid = {"features_readable": True, "sync_enabled": True, "password_sync": False}
    assert fed_signals._no_password_hash_sync(_data(trusts=[], hybrid=hybrid), _ctx()) == []
    out = fed_signals._no_password_hash_sync(_data(trusts=[_trust()], hybrid=hybrid), _ctx())
    assert len(out) == 1


def test_password_hash_sync_on_is_not_a_finding():
    hybrid = {"features_readable": True, "sync_enabled": True, "password_sync": True}
    assert fed_signals._no_password_hash_sync(_data(trusts=[_trust()], hybrid=hybrid), _ctx()) == []


def test_unreadable_sync_configuration_is_unavailable_not_clean():
    """"We could not look" must never score the same as "we looked and it was fine"."""
    hybrid = {"features_readable": False, "sync_enabled": True}
    with pytest.raises(SignalUnavailable):
        fed_signals._no_password_hash_sync(_data(trusts=[_trust()], hybrid=hybrid), _ctx())


def test_an_unreadable_domain_list_makes_every_federation_signal_unavailable():
    data = _data(trusts=[], readable=False, blind_reason="Domain.Read.All")
    for evaluate in (fed_signals._federated_mfa_trusted, fed_signals._federation_cert_expiry,
                     fed_signals._federation_cert_rollover, fed_signals._federated_domain,
                     fed_signals._federation_protocol, fed_signals._no_password_hash_sync):
        with pytest.raises(SignalUnavailable):
            evaluate(data, _ctx())


def test_a_cloud_only_tenant_produces_no_federation_findings():
    data = _data(trusts=[])
    assert fed_signals._federated_mfa_trusted(data, _ctx()) == []
    assert fed_signals._federated_domain(data, _ctx()) == []
    assert fed_signals._federation_protocol(data, _ctx()) == []


def test_the_informational_row_carries_the_blast_radius():
    trust = _trust(user_count=4200, user_share=0.7285)
    out = fed_signals._federated_domain(_data(trusts=[trust]), _ctx())
    assert "4,200 user(s)" in out[0]["title"]
    assert "73%" in out[0]["title"]


def test_wsfed_is_noted_and_saml_is_not():
    assert len(fed_signals._federation_protocol(_data(trusts=[_trust()]), _ctx())) == 1
    saml = _trust(protocol="saml")
    assert fed_signals._federation_protocol(_data(trusts=[saml]), _ctx()) == []


# ------------------------------------------------------------------------ normalising
def test_normalise_federation_derives_vendor_host_and_behaviour():
    row = fed.normalize_federation("contoso.com", {
        "issuerUri": "http://contoso.com/PingFederate",
        "passiveSignInUri": "https://pf.contoso.com/idp/a/prp.wsf",
        "preferredAuthenticationProtocol": "wsFed",
        "federatedIdpMfaBehavior": "",
        "signingCertificateUpdateStatus": {"certificateUpdateResult": "NotFound", "lastRunDateTime": "x"},
    })
    assert row["vendor"]["label"] == "PingFederate"
    assert row["host"] == "pf.contoso.com"
    assert row["mfa_behaviour"]["trusted"] is True
    assert row["auto_rollover"]["healthy"] is False


def test_population_counts_only_the_domain_asked_for():
    users = [{"upn": "a@contoso.com"}, {"upn": "b@CONTOSO.COM"}, {"upn": "c@other.com"}, {}]
    assert fed.population(users, "contoso.com") == 2
    assert fed.population(users, "other.com") == 1
    assert fed.population(users, "absent.com") == 0


# --------------------------------------------------------------- external providers
def test_normalise_idp_labels_the_odata_type():
    row = fed.normalize_idp({
        "@odata.type": "#microsoft.graph.socialIdentityProvider",
        "id": "Google-OAUTH", "displayName": "Google", "clientId": "abc.apps.googleusercontent.com",
        "identityProviderType": "Google",
    })
    assert row["kind"] == "socialIdentityProvider"
    assert row["kind_label"] == "Social"
    assert row["identity_provider_type"] == "Google"
    assert row["client_id"] == "abc.apps.googleusercontent.com"


def test_normalise_idp_falls_back_to_the_raw_type_then_a_generic_label():
    """An unrecognized provider is still named, and a typeless one is not blank."""
    assert fed.normalize_idp({"@odata.type": "#microsoft.graph.brandNewIdp"})["kind_label"] == "brandNewIdp"
    assert fed.normalize_idp({})["kind_label"] == "Identity provider"


def test_normalise_idp_never_carries_a_client_secret():
    """The secret is not requested, and must not survive even if Graph volunteers one."""
    row = fed.normalize_idp({
        "@odata.type": "#microsoft.graph.socialIdentityProvider",
        "id": "x", "clientId": "public-id", "clientSecret": "super-secret-value",
    })
    assert "super-secret-value" not in str(row)
    assert not any("secret" in k for k in row)
