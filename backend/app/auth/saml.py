"""SAML 2.0 SP — covers Microsoft Entra ID, Okta, ADFS, PingFederate, etc.

SP-initiated SSO: we emit an AuthnRequest (HTTP-Redirect) and validate the signed
assertion posted back to the ACS. Signature verification uses signxml (lxml +
cryptography wheels — no native xmlsec build needed). Users are JIT-provisioned via
app.auth.provisioning.

Config (config_json on IdentityProvider):
  entity_id        : the IdP's EntityID (issuer expected in the assertion)
  sso_url          : IdP SSO redirect endpoint
  certificate      : the IdP's signing certificate (PEM or base64 DER)
  email_attr       : assertion attribute name for email (default common URIs tried)
  name_attr        : assertion attribute name for display name
  group_attr       : assertion attribute name carrying group memberships
  group_role_map   : { "<idp group>": "<role name>" }
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from lxml import etree
from signxml import XMLVerifier

from app.core.crypto import decrypt, encrypt

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}

# How long an in-flight SP-initiated request stays valid (the AuthnRequest ID is carried
# in an encrypted, single-use cookie so the ACS can bind the response via InResponseTo).
RELAY_TTL_SECONDS = 600
# Tolerance for IdP/SP clock drift when checking assertion validity windows.
_CLOCK_SKEW = timedelta(seconds=120)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def encode_relay(payload: dict[str, Any]) -> str:
    """Encrypt the in-flight SAML request state (AuthnRequest ID + idp) for the cookie."""
    return encrypt(json.dumps({**payload, "ts": int(time.time())}))


def decode_relay(token: str) -> dict[str, Any] | None:
    """Decrypt + freshness-check the SAML request-state cookie. None if invalid/expired."""
    try:
        data = json.loads(decrypt(token))
    except Exception:  # noqa: BLE001
        return None
    if int(time.time()) - int(data.get("ts", 0)) > RELAY_TTL_SECONDS:
        return None
    return data


def _pem(cert: str) -> str:
    cert = (cert or "").strip()
    if "BEGIN CERTIFICATE" in cert:
        return cert
    # Assume base64 DER (as Entra/Okta metadata often provide).
    body = "".join(cert.split())
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def test_config(idp_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort validation of a SAML provider config. Checks required fields are present,
    the signing certificate parses (and isn't expired), and the SSO URL is well-formed. No
    live SSO round-trip is attempted (that needs a real user)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "critical": critical})

    entity_id = (idp_cfg.get("entity_id") or "").strip()
    sso_url = (idp_cfg.get("sso_url") or "").strip()
    cert = (idp_cfg.get("certificate") or "").strip()

    add("IdP Entity ID set", bool(entity_id), entity_id or "The IdP Entity ID (issuer) is required.")
    add("IdP SSO URL set", bool(sso_url), sso_url or "The IdP SSO URL is required.")
    if sso_url:
        ok_url = sso_url.lower().startswith(("https://", "http://"))
        add("SSO URL is a valid URL", ok_url,
            "SSO URL should be an https:// endpoint." if not ok_url else sso_url, critical=False)

    add("Signing certificate provided", bool(cert), "Paste the IdP's signing certificate (PEM or base64 DER).")
    if cert:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes

            certificate = x509.load_pem_x509_certificate(_pem(cert).encode("utf-8"))
            add("Certificate parses", True, "Signing certificate loaded successfully.")
            try:
                from datetime import datetime, timezone
                not_after = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after.replace(tzinfo=timezone.utc)
                not_before = getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                in_window = not_before <= now <= not_after
                add("Certificate within validity period", in_window,
                    f"Valid {not_before.date()} → {not_after.date()}." if in_window
                    else f"Out of window: {not_before.date()} → {not_after.date()}.", critical=False)
            except Exception:  # noqa: BLE001
                pass
            try:
                fp = certificate.fingerprint(hashes.SHA256()).hex()
                add("Certificate fingerprint (SHA-256)", True, ":".join(fp[i:i+2] for i in range(0, 12, 2)) + "…", critical=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            add("Certificate parses", False, f"Could not parse the certificate: {str(e)[:160]}")
    return checks


def sp_entity_id(public_base_url: str) -> str:
    # The auth router is mounted under the global ``/api`` prefix, so every SAML route
    # (metadata + ACS) lives under ``/api/auth/saml/...``. The SP EntityID + ACS URL are
    # built off the public base URL and MUST include ``/api`` so the IdP posts the signed
    # assertion back to a real route and the AudienceRestriction / Recipient checks match.
    return public_base_url.rstrip("/") + "/api/auth/saml/metadata"


def acs_url(public_base_url: str, idp_id: str) -> str:
    return public_base_url.rstrip("/") + f"/api/auth/saml/{idp_id}/acs"


def sp_metadata(public_base_url: str, idp_id: str) -> str:
    """SP metadata XML to hand to the IdP admin."""
    entity = sp_entity_id(public_base_url)
    acs = acs_url(public_base_url, idp_id)
    return (
        '<?xml version="1.0"?>'
        f'<md:EntityDescriptor xmlns:md="{NS["md"]}" entityID="{entity}">'
        '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true"'
        ' protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '<md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress</md:NameIDFormat>'
        f'<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
        f' Location="{acs}" index="0" isDefault="true"/>'
        "</md:SPSSODescriptor></md:EntityDescriptor>"
    )


def build_authn_request(idp_cfg: dict[str, Any], public_base_url: str, idp_id: str) -> tuple[str, str]:
    """Return ``(redirect_url, request_id)`` for an SP-initiated AuthnRequest.

    The caller persists ``request_id`` in a single-use encrypted cookie so the ACS can
    bind the response to this request (``InResponseTo``) and reject replays / unsolicited
    responses."""
    req_id = "_" + secrets.token_hex(16)
    issue_instant = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    acs = acs_url(public_base_url, idp_id)
    issuer = sp_entity_id(public_base_url)
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}"'
        f' ID="{req_id}" Version="2.0" IssueInstant="{issue_instant}"'
        f' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
        f' AssertionConsumerServiceURL="{acs}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress"'
        ' AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )
    # raw-deflate (wbits=-15), per the SAML HTTP-Redirect binding.
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = co.compress(xml.encode("utf-8")) + co.flush()
    encoded = base64.b64encode(deflated).decode("ascii")
    params = {"SAMLRequest": encoded}
    url = idp_cfg["sso_url"] + ("&" if "?" in idp_cfg["sso_url"] else "?") + urlencode(params)
    return url, req_id


def validate_response(
    saml_response_b64: str,
    idp_cfg: dict[str, Any],
    *,
    sp_entity_id: str = "",
    acs_url: str = "",
    expected_in_response_to: str | None = None,
) -> dict[str, Any]:
    """Verify signature + conditions and extract identity from the SAML Response.

    SECURITY (XML Signature Wrapping defense): identity is extracted **only** from the
    element the signature actually covers (``signxml``'s returned ``signed_xml`` subtree),
    never from a re-parse of the raw document. An attacker who wraps a signed element
    around an injected, unsigned assertion therefore can't get that injected assertion
    honoured — we only ever read the verified subtree.

    Additional binding checks (replay / audience confusion defense):
    - exactly one signed reference / assertion is required;
    - issuer must match the configured IdP entityID;
    - NotBefore / NotOnOrAfter (with small clock skew) on Conditions and on the
      SubjectConfirmationData;
    - AudienceRestriction must include our SP entityID (when present);
    - SubjectConfirmationData Recipient must equal our ACS URL (when present);
    - SubjectConfirmationData InResponseTo must equal the AuthnRequest we issued
      (``expected_in_response_to``) — this rejects replayed and unsolicited responses.

    Raises on any validation failure. Returns the identity dict."""
    raw = base64.b64decode(saml_response_b64)
    cert_pem = _pem(idp_cfg.get("certificate", ""))
    if not cert_pem.strip():
        raise RuntimeError("IdP signing certificate is not configured.")

    # Verify the XML signature against the configured cert. We require EXACTLY ONE signed
    # reference; multiple signatures are a classic wrapping vector.
    try:
        result = XMLVerifier().verify(raw, x509_cert=cert_pem)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SAML signature validation failed: {exc}") from exc
    if isinstance(result, list):
        if len(result) != 1:
            raise RuntimeError("Expected exactly one signed element in the SAML response.")
        result = result[0]
    verified = getattr(result, "signed_xml", None)
    if verified is None:
        raise RuntimeError("SAML signature did not cover any element.")

    # Resolve the verified Assertion. The signature covers either the Assertion directly
    # (WantAssertionsSigned) or the Response that contains exactly one Assertion. In both
    # cases we ONLY look inside the verified subtree.
    qn = etree.QName(verified.tag)
    if qn.localname == "Assertion":
        assertion = verified
    elif qn.localname == "Response":
        found = verified.findall("saml:Assertion", NS)
        if len(found) != 1:
            raise RuntimeError("Expected exactly one assertion in the signed response.")
        assertion = found[0]
    else:
        raise RuntimeError("Signed element is neither a SAML Response nor a SAML Assertion.")

    now = _now()
    fmt = "%Y-%m-%dT%H:%M:%SZ"

    def _parse(t: str) -> datetime:
        t = t.split(".")[0].rstrip("Z") + "Z"
        return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)

    # Issuer check (read from the verified assertion).
    issuer_el = assertion.find("saml:Issuer", NS)
    expected_issuer = idp_cfg.get("entity_id", "")
    if expected_issuer and (issuer_el is None or (issuer_el.text or "").strip() != expected_issuer):
        raise RuntimeError("SAML issuer mismatch.")

    # Conditions: validity window + audience restriction.
    cond = assertion.find("saml:Conditions", NS)
    if cond is not None:
        nb = cond.get("NotBefore")
        na = cond.get("NotOnOrAfter")
        if nb and now < _parse(nb) - _CLOCK_SKEW:
            raise RuntimeError("SAML assertion not yet valid.")
        if na and now >= _parse(na) + _CLOCK_SKEW:
            raise RuntimeError("SAML assertion expired.")
        audiences = [
            (a.text or "").strip()
            for a in cond.findall(".//saml:AudienceRestriction/saml:Audience", NS)
            if (a.text or "").strip()
        ]
        if sp_entity_id and audiences and sp_entity_id not in audiences:
            raise RuntimeError("SAML audience restriction does not include this service provider.")

    # Subject confirmation: recipient binding + window + InResponseTo (replay defense).
    scd = assertion.find(
        ".//saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", NS
    )
    in_resp = scd.get("InResponseTo") if scd is not None else None
    if scd is not None:
        recipient = scd.get("Recipient")
        if acs_url and recipient and recipient != acs_url:
            raise RuntimeError("SAML SubjectConfirmation recipient mismatch.")
        scd_na = scd.get("NotOnOrAfter")
        if scd_na and now >= _parse(scd_na) + _CLOCK_SKEW:
            raise RuntimeError("SAML subject confirmation expired.")
    if expected_in_response_to is not None:
        # SP-initiated SSO: the assertion MUST be a response to the request we issued.
        if not in_resp or in_resp != expected_in_response_to:
            raise RuntimeError("SAML InResponseTo mismatch (replayed or unsolicited response).")

    # NameID (subject) — read from the verified subtree only.
    nameid_el = assertion.find(".//saml:Subject/saml:NameID", NS)
    name_id = (nameid_el.text or "").strip() if nameid_el is not None else ""

    # Attributes (verified subtree).
    attrs: dict[str, list[str]] = {}
    for attr in assertion.findall(".//saml:AttributeStatement/saml:Attribute", NS):
        name = attr.get("Name") or attr.get("FriendlyName") or ""
        vals = [
            (v.text or "").strip()
            for v in attr.findall("saml:AttributeValue", NS)
            if (v.text or "").strip()
        ]
        if name:
            attrs[name] = vals

    def _first(*names: str) -> str:
        for n in names:
            if attrs.get(n):
                return attrs[n][0]
        return ""

    email = _first(
        idp_cfg.get("email_attr", ""),
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "email",
        "mail",
    ) or name_id
    display = _first(
        idp_cfg.get("name_attr", ""),
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "displayName",
        "name",
    )
    group_attr = idp_cfg.get("group_attr") or (
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
    )
    groups = attrs.get(group_attr, []) or attrs.get("groups", []) or attrs.get("Groups", [])

    return {
        "external_id": name_id or email,
        "email": email,
        "display_name": display,
        "groups": list(groups),
        "assertion_id": assertion.get("ID") or "",
        "in_response_to": in_resp or "",
        # email comes from a cryptographically-signed assertion → treat as verified.
        "email_verified": True,
    }
