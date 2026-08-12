"""Safe Azure Portal deep links derived from trusted cloud metadata and ARM resource IDs.

Provider payloads never get to supply a complete URL.  A portal link is useful only when it
cannot become an open redirect, a javascript URL, or a confidently-wrong link into another
Azure cloud, so callers pass an ARM id and this module constructs the URL after validation.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

_SUBSCRIPTION_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/.*)?$",
    re.IGNORECASE,
)

PORTAL_HOSTS = {
    "azurecloud": "portal.azure.com",
    "public": "portal.azure.com",
    "azurepublic": "portal.azure.com",
    "azureusgovernment": "portal.azure.us",
    "usgovernment": "portal.azure.us",
    "government": "portal.azure.us",
    "azurechinacloud": "portal.azure.cn",
    "china": "portal.azure.cn",
}


def _cloud_token(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def portal_host(connection: dict[str, Any] | None = None) -> str:
    """Trusted portal host for a connection, defaulting to public Azure.

    Existing connection registries predate cloud metadata, so absence means AzureCloud.  An
    explicit but unknown value fails closed instead of silently sending a sovereign customer
    to the public portal.
    """
    conn = connection or {}
    raw = conn.get("azure_cloud") or conn.get("cloud") or conn.get("environment")
    if raw in (None, ""):
        return PORTAL_HOSTS["azurecloud"]
    return PORTAL_HOSTS.get(_cloud_token(raw), "")


def valid_resource_id(resource_id: Any) -> str:
    """Canonical linkable ARM id, or ``""`` when the input is not defensible."""
    value = str(resource_id or "").strip()
    if not value or any(ch in value for ch in ("?", "#", "\r", "\n", "\t", "\\")):
        return ""
    if "://" in value or not _SUBSCRIPTION_ID.fullmatch(value):
        return ""
    return value.rstrip("/")


def resource_url_for_host(resource_id: Any, host: Any) -> str:
    """Azure Portal Overview URL using an already trusted host value."""
    rid = valid_resource_id(resource_id)
    portal = str(host or "").lower()
    if not rid or portal not in set(PORTAL_HOSTS.values()):
        return ""
    # quote(..., safe="/") preserves the ARM hierarchy while escaping spaces and punctuation.
    return f"https://{portal}/#@/resource{quote(rid, safe='/:()')}/overview"


def resource_url(resource_id: Any, connection: dict[str, Any] | None = None) -> str:
    """Azure Portal Overview URL for a validated ARM resource id."""
    return resource_url_for_host(resource_id, portal_host(connection))


def subscription_url(subscription_id: Any, connection: dict[str, Any] | None = None) -> str:
    value = str(subscription_id or "").strip()
    rid = f"/subscriptions/{value}"
    return resource_url(rid, connection)