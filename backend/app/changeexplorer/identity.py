"""Actor identity resolution for the Change Explorer — the trust layer of a forensic screen.

This turns the raw ``caller`` + ``claims`` an Activity Log event carries into a *trustworthy*
actor: a refined kind (User / App / Managed Identity / Azure platform / Unknown), a human-friendly
display name resolved from Microsoft Graph (so a bare object-id like ``e1dd8f92-…`` becomes
"Contoso Deploy SPN"), the originating IP, and any on-behalf-of user.

Everything here is **best-effort and fail-open**: if Graph is unreachable or the connection lacks
``Directory.Read.All``, resolution silently degrades to the object-id (never blocks or fails a run).
Resolutions are cached per (tenant, id) with a TTL so repeated runs don't re-hit Graph.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("app.changeexplorer.identity")

# --------------------------------------------------------------------------- claim keys
# Activity Log ``claims`` is a flat dict mixing short names (idtyp, appid, ipaddr) and the long
# WS-Fed/SAML claim URIs. We read both spellings defensively.
_UPN_KEYS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "name",
    "preferred_username",
)
_NAME_KEYS = (
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.microsoft.com/identity/claims/displayname",
)
_APPID_KEYS = (
    "appid",
    "http://schemas.microsoft.com/identity/claims/appid",
    "azp",
    "appId",
)
_IP_KEYS = (
    "ipaddr",
    "http://schemas.microsoft.com/2008/06/identity/claims/ipaddr",
    "ip",
)
_OID_KEYS = (
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
    "oid",
)

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Well-known first-party Microsoft appIds that show up as the "caller" for platform-initiated
# control-plane writes (regional network manager, policy remediation, backup, etc.). These are
# Azure acting on its own behalf — labelling them "unknown actor" is misleading noise on a
# forensic screen, so we mark them as the platform.
_PLATFORM_APPIDS = {
    "37182072-3c9c-4f6a-a4b3-b3f91cacffce",  # Azure Network (RNM-style) services
    "7319c514-987d-4e9b-ac3d-d38c4f427f4c",  # Azure Policy / remediation
    "abfa0a7c-a6b6-4736-8310-5855508787cd",  # Microsoft Azure App Service
    "262044b1-e2ce-469f-a196-69ab7ada62d3",  # Backup management
    "e406a681-f3d4-42a8-90b6-c2b029497af1",  # Azure Storage
}
_PLATFORM_CALLER_HINTS = (
    "@cloudapp.net", "microsoft.com/azure", "policyinsights", "regionalnetworkmanager",
)


def _claim(claims: dict[str, Any] | None, keys: tuple[str, ...]) -> str:
    if not claims:
        return ""
    for k in keys:
        v = claims.get(k)
        if v:
            return str(v)
    return ""


def is_guid(s: str) -> bool:
    return bool(_GUID_RE.match((s or "").strip()))


def classify_actor(caller: str, claims: dict[str, Any] | None,
                   correlation_id: str = "") -> tuple[str, bool]:
    """Refine an actor into ``(kind, is_platform)``.

    ``kind`` ∈ {User, ServicePrincipal, ManagedIdentity, AzurePolicy, AzurePlatform, System,
    Unknown}. ``is_platform`` is True for Azure-initiated/automation writes that should NOT be
    flagged as a suspicious "unknown actor".
    """
    c = (caller or "").strip()
    cl = c.lower()
    idtyp = (_claim(claims, ("idtyp",)) or "").lower()
    appid = _claim(claims, _APPID_KEYS)

    # Platform / automation: empty caller, the zero correlation id, a known first-party appId, or
    # an obvious platform caller hint. These are Azure acting on its own behalf.
    is_platform = False
    if not c or (correlation_id or "").strip() in ("", _ZERO_GUID):
        is_platform = True
    if appid and appid.lower() in _PLATFORM_APPIDS:
        is_platform = True
    if any(h in cl for h in _PLATFORM_CALLER_HINTS):
        is_platform = True

    if "@" in cl:
        return "User", False
    if idtyp == "user":
        return "User", False
    if idtyp == "app":
        return ("AzurePlatform" if is_platform else "ServicePrincipal"), is_platform

    # Managed identity heuristic: MSI tokens commonly carry xms_mirid / a MI marker, or the caller
    # is a GUID with no UPN and an appid present.
    if claims and (claims.get("xms_mirid") or claims.get("xms_az_rid")):
        return "ManagedIdentity", is_platform

    if is_platform:
        return "AzurePlatform", True
    if is_guid(c):
        # A bare object-id without a UPN is an app/SPN/MI; default to ServicePrincipal.
        return "ServicePrincipal", False
    if not c:
        return "Unknown", is_platform
    return "Unknown", is_platform


def extract_actor_meta(caller: str, claims: dict[str, Any] | None,
                       correlation_id: str = "") -> dict[str, Any]:
    """Everything we can learn about an actor from one event's caller + claims, WITHOUT Graph.

    Returns a dict: {kind, is_platform, object_id, app_id, ip, on_behalf_of}.
    """
    kind, is_platform = classify_actor(caller, claims, correlation_id)
    object_id = _claim(claims, _OID_KEYS)
    if not object_id and is_guid(caller):
        object_id = caller.strip()
    app_id = _claim(claims, _APPID_KEYS)
    ip = _claim(claims, _IP_KEYS)
    # On-behalf-of: when an app/SPN made the call but a user principal name is present in claims.
    on_behalf_of = ""
    if kind in ("ServicePrincipal", "ManagedIdentity", "AzurePlatform"):
        upn = _claim(claims, _UPN_KEYS)
        if upn and "@" in upn:
            on_behalf_of = upn
    return {
        "kind": kind, "is_platform": is_platform, "object_id": object_id,
        "app_id": app_id, "ip": ip, "on_behalf_of": on_behalf_of,
    }


# --------------------------------------------------------------------------- Graph resolution
_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
_CACHE_TTL = 12 * 3600  # 12 hours — directory names rarely change within a session


def _cache_get(tenant: str, key: str) -> dict[str, str] | None:
    hit = _CACHE.get((tenant, key))
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(tenant: str, key: str, value: dict[str, str]) -> None:
    _CACHE[(tenant, key)] = (time.time(), value)


async def resolve_display_names(
    object_ids: list[str], app_ids: list[str], connection: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, str]], str]:
    """Resolve directory object-ids (and appIds) to ``{id: {display, kind}}`` via Microsoft Graph.

    Best-effort + fail-open: returns ``({}, note)`` when Graph is unreachable / unauthorized, so
    the caller keeps the raw object-ids. Results are cached per (tenant, id). ``kind`` mirrors the
    Graph @odata.type (user / servicePrincipal / group).
    """
    out: dict[str, dict[str, str]] = {}
    tenant = (connection or {}).get("tenant_id", "") or "_"

    # Partition into cached vs to-fetch.
    want_oids = [i for i in {x.strip() for x in object_ids if is_guid(x or "")}]
    want_apps = [a for a in {x.strip() for x in app_ids if is_guid(x or "")}]
    to_fetch_oids: list[str] = []
    for oid in want_oids:
        c = _cache_get(tenant, oid)
        if c is not None:
            if c:
                out[oid] = c
        else:
            to_fetch_oids.append(oid)
    to_fetch_apps: list[str] = []
    for aid in want_apps:
        c = _cache_get(tenant, "app:" + aid)
        if c is not None:
            if c:
                out[aid] = c
        else:
            to_fetch_apps.append(aid)

    if not to_fetch_oids and not to_fetch_apps:
        return out, ""

    from app.azure.credentials import get_graph_token

    token, terr = await get_graph_token(connection or {})
    if not token:
        return out, (
            "Identity names not resolved — this connection has no Microsoft Graph access "
            f"({terr or 'no Graph token'}). Object-ids are shown as-is."
        )

    import httpx

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # directoryObjects/getByIds resolves users, servicePrincipals and groups in one batch.
            # NOTE: managed identities are themselves ``servicePrincipal`` objects — there is no
            # ``managedIdentity`` directory type, and including it makes Graph reject the whole
            # request with 400 "Invalid resource type specified".
            for chunk in _chunks(to_fetch_oids, 900):
                body = {"ids": chunk, "types": ["user", "servicePrincipal", "group"]}
                resp = await client.post(
                    "https://graph.microsoft.com/v1.0/directoryObjects/getByIds",
                    headers=headers, json=body,
                )
                if resp.status_code != 200:
                    return out, _graph_error_note(resp.status_code, resp.text)
                for obj in (resp.json().get("value", []) or []):
                    oid = obj.get("id", "")
                    if not oid:
                        continue
                    rec = {
                        "display": obj.get("displayName") or obj.get("appDisplayName")
                        or obj.get("userPrincipalName") or oid,
                        "kind": _odata_kind(obj.get("@odata.type", "")),
                    }
                    out[oid] = rec
                    _cache_put(tenant, oid, rec)
                # Cache the ids Graph didn't return (deleted / cross-tenant) as empty so we don't
                # re-query them every run.
                returned = {o.get("id", "") for o in (resp.json().get("value", []) or [])}
                for missing in chunk:
                    if missing not in returned:
                        _cache_put(tenant, missing, {})

            # appId -> servicePrincipal (the Activity Log appid claim is the app id, not object id).
            for aid in to_fetch_apps:
                resp = await client.get(
                    "https://graph.microsoft.com/v1.0/servicePrincipals",
                    headers=headers,
                    params={"$filter": f"appId eq '{aid}'", "$select": "id,displayName,appId"},
                )
                if resp.status_code == 200:
                    vals = resp.json().get("value", []) or []
                    if vals:
                        sp = vals[0]
                        rec = {"display": sp.get("displayName") or aid, "kind": "ServicePrincipal"}
                        out[aid] = rec
                        _cache_put(tenant, "app:" + aid, rec)
                    else:
                        _cache_put(tenant, "app:" + aid, {})
    except httpx.HTTPError as e:  # noqa: BLE001
        return out, f"Identity resolution error ({e}); object-ids are shown as-is."

    return out, ""


def _odata_kind(odata_type: str) -> str:
    t = (odata_type or "").lower()
    if "serviceprincipal" in t:
        return "ServicePrincipal"
    if "managedidentity" in t:
        return "ManagedIdentity"
    if "group" in t:
        return "Group"
    if "user" in t:
        return "User"
    return ""


def _graph_error_note(status: int, body: str) -> str:
    """A precise, non-misleading note for a failed Graph resolution.

    Only 401/403 imply a permission problem — do NOT tell the user to grant Directory.Read.All on
    a 400 (that's a malformed request, not a missing permission)."""
    detail = ""
    try:
        import json as _json

        detail = (_json.loads(body or "{}").get("error", {}) or {}).get("message", "")
    except (ValueError, TypeError):
        detail = ""
    if status in (401, 403):
        return (f"Identity resolution via Graph was denied ({status}); object-ids are shown as-is. "
                "Grant the connection Directory.Read.All (admin-consented) to resolve names.")
    suffix = f" — {detail}" if detail else ""
    return f"Identity resolution via Graph failed ({status}{suffix}); object-ids are shown as-is."


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
