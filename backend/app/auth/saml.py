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
import secrets
import zlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from lxml import etree
from signxml import XMLVerifier

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pem(cert: str) -> str:
    cert = (cert or "").strip()
    if "BEGIN CERTIFICATE" in cert:
        return cert
    # Assume base64 DER (as Entra/Okta metadata often provide).
    body = "".join(cert.split())
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def sp_entity_id(public_base_url: str) -> str:
    return public_base_url.rstrip("/") + "/auth/saml/metadata"


def acs_url(public_base_url: str, idp_id: str) -> str:
    return public_base_url.rstrip("/") + f"/auth/saml/{idp_id}/acs"


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


def build_authn_request(idp_cfg: dict[str, Any], public_base_url: str, idp_id: str) -> str:
    """Return the IdP redirect URL with a deflated, base64'd AuthnRequest (HTTP-Redirect)."""
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
    return idp_cfg["sso_url"] + ("&" if "?" in idp_cfg["sso_url"] else "?") + urlencode(params)


def validate_response(saml_response_b64: str, idp_cfg: dict[str, Any]) -> dict[str, Any]:
    """Verify signature + conditions and extract identity from the SAML Response.

    Raises on any validation failure. Returns identity dict."""
    raw = base64.b64decode(saml_response_b64)
    cert_pem = _pem(idp_cfg.get("certificate", ""))
    if not cert_pem.strip():
        raise RuntimeError("IdP signing certificate is not configured.")

    # Verify the XML signature against the configured cert. signxml returns the verified
    # element(s); we then re-parse for attribute extraction.
    try:
        verified = XMLVerifier().verify(raw, x509_cert=cert_pem).signed_xml
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SAML signature validation failed: {exc}") from exc

    # The signed element may be the Response or the Assertion. Re-parse the full doc to
    # read conditions/attributes (signature already verified above).
    doc = etree.fromstring(raw)
    assertion = doc.find(".//saml:Assertion", NS)
    if assertion is None and verified is not None and verified.tag.endswith("}Assertion"):
        assertion = verified
    if assertion is None:
        raise RuntimeError("No assertion found in the SAML response.")

    # Issuer check.
    issuer_el = assertion.find("saml:Issuer", NS)
    expected_issuer = idp_cfg.get("entity_id", "")
    if expected_issuer and (issuer_el is None or (issuer_el.text or "").strip() != expected_issuer):
        raise RuntimeError("SAML issuer mismatch.")

    # Conditions (NotBefore / NotOnOrAfter).
    cond = assertion.find("saml:Conditions", NS)
    now = _now()
    if cond is not None:
        nb = cond.get("NotBefore")
        na = cond.get("NotOnOrAfter")
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        def _parse(t: str) -> datetime:
            t = t.split(".")[0].rstrip("Z") + "Z"
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)

        if nb and now < _parse(nb):
            raise RuntimeError("SAML assertion not yet valid.")
        if na and now >= _parse(na):
            raise RuntimeError("SAML assertion expired.")

    # NameID (subject).
    nameid_el = assertion.find(".//saml:Subject/saml:NameID", NS)
    name_id = (nameid_el.text or "").strip() if nameid_el is not None else ""

    # Attributes.
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
    }
