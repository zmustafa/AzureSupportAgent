"""Translate an Azure connection into (a) child-process env for the MCP server and
(b) an ARM access token for the app's own multi-tenant discovery / health checks.

This is the bridge between the connection registry and Azure. It supports four auth
methods (see app.core.azure_connections). Service-principal methods drive the Azure
MCP server fully through standard ``AZURE_*`` environment variables (EnvironmentCredential
inside DefaultAzureCredential). The pasted-token method powers cross-tenant discovery
and scoping immediately via ARM REST.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import httpx

_ARM_SCOPE = "https://management.azure.com/.default"
_ARM_RESOURCE = "https://management.azure.com"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_GRAPH_RESOURCE = "https://graph.microsoft.com"
_LOGIN = "https://login.microsoftonline.com"


def build_mcp_env(conn: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Return (env_overrides, cleanup_paths) for spawning the MCP server bound to this
    connection's tenant/identity. cleanup_paths are temp files (e.g. a cert) the caller
    should delete after the session ends.
    """
    env: dict[str, str] = {}
    cleanup: list[str] = []
    method = conn.get("auth_method", "")
    tenant = conn.get("tenant_id", "")
    sub = conn.get("default_subscription", "")
    if tenant:
        env["AZURE_TENANT_ID"] = tenant
    if sub:
        env["AZURE_SUBSCRIPTION_ID"] = sub

    if method == "service_principal":
        env["AZURE_CLIENT_ID"] = conn.get("client_id", "")
        env["AZURE_CLIENT_SECRET"] = conn.get("client_secret", "")
        # Force the SP (EnvironmentCredential) instead of any host-pinned credential.
        env["AZURE_TOKEN_CREDENTIALS"] = "EnvironmentCredential"
    elif method == "service_principal_cert":
        pem = conn.get("certificate_pem", "")
        path = ""
        if pem:
            fd = tempfile.NamedTemporaryFile(
                "w", suffix=".pem", delete=False, encoding="utf-8"
            )
            fd.write(pem)
            fd.close()
            path = fd.name
            cleanup.append(path)
        env["AZURE_CLIENT_ID"] = conn.get("client_id", "")
        env["AZURE_CLIENT_CERTIFICATE_PATH"] = path
        env["AZURE_TOKEN_CREDENTIALS"] = "EnvironmentCredential"
    elif method == "az_cli_token":
        # The MCP server can't consume a raw token directly; bind it to the tenant and
        # let it use the host Azure CLI for that tenant. The pasted token still powers
        # discovery/scoping/health via ARM REST in this app.
        env["AZURE_TOKEN_CREDENTIALS"] = "AzureCliCredential"
    else:  # default_chain ("Host identity")
        # Use the platform managed identity in the cloud (Container Apps / App Service /
        # VM), and the host az login locally. Pin DefaultAzureCredential to the right
        # credential so it doesn't fall through to interactive/dev credentials.
        if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
            env["AZURE_TOKEN_CREDENTIALS"] = "ManagedIdentityCredential"
            cid = _managed_identity_client_id(conn)
            if cid:
                # Selects the user-assigned identity; harmless/ignored for system-assigned.
                env["AZURE_CLIENT_ID"] = cid
        else:
            env["AZURE_TOKEN_CREDENTIALS"] = "AzureCliCredential"
    return env, cleanup


def _token_expired(expires_on: str) -> bool:
    if not expires_on:
        return False
    s = str(expires_on).strip()
    # Unix epoch seconds (UTC) — az's `expires_on` field. Unambiguous across timezones,
    # so this is the preferred form (the human-readable `expiresOn` is LOCAL time and
    # would be mis-compared against the server's UTC clock — e.g. inside a container).
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc) <= datetime.now(timezone.utc)
        except (ValueError, OverflowError, OSError):
            return False
    try:
        # Legacy fallback: az's local-time "2026-06-06 12:00:00.000000" or ISO string.
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt <= datetime.now()
            except ValueError:
                continue
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
        return dt <= now
    except (ValueError, TypeError):
        return False


async def _sp_secret_token(
    tenant: str, client_id: str, secret: str, scope: str = _ARM_SCOPE
) -> tuple[str | None, str | None]:
    url = f"{_LOGIN}/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": secret,
        "scope": scope,
        "grant_type": "client_credentials",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            detail = resp.json().get("error_description", resp.text)[:300]
            return None, f"Token request failed ({resp.status_code}): {detail}"
        return resp.json().get("access_token"), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"Token request error: {e}"


def _build_cert_assertion(tenant: str, client_id: str, pem: str) -> tuple[str | None, str | None]:
    try:
        import jwt  # PyJWT
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509 import load_pem_x509_certificate

        cert = load_pem_x509_certificate(pem.encode("utf-8"))
        thumbprint = cert.fingerprint(hashes.SHA1())
        x5t = base64.urlsafe_b64encode(thumbprint).decode("utf-8").rstrip("=")
        now = int(time.time())
        claims = {
            "aud": f"{_LOGIN}/{tenant}/oauth2/v2.0/token",
            "iss": client_id,
            "sub": client_id,
            "jti": base64.urlsafe_b64encode(str(now).encode()).decode(),
            "nbf": now,
            "exp": now + 600,
        }
        assertion = jwt.encode(
            claims, pem, algorithm="RS256", headers={"x5t": x5t}
        )
        return assertion, None
    except Exception as e:  # noqa: BLE001 - cert parsing/signing best-effort
        return None, f"Certificate assertion failed: {e}"


async def _sp_cert_token(
    tenant: str, client_id: str, pem: str, scope: str = _ARM_SCOPE
) -> tuple[str | None, str | None]:
    assertion, err = _build_cert_assertion(tenant, client_id, pem)
    if err:
        return None, err
    url = f"{_LOGIN}/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "scope": scope,
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            detail = resp.json().get("error_description", resp.text)[:300]
            return None, f"Token request failed ({resp.status_code}): {detail}"
        return resp.json().get("access_token"), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"Token request error: {e}"


async def _cli_token(tenant: str, resource: str = _ARM_RESOURCE) -> tuple[str | None, str | None]:
    """Fallback for default_chain: shell out to the host Azure CLI for a token."""
    import shutil

    az = shutil.which("az")
    if not az:
        return None, "Azure CLI (az) not found on the host."
    args = [
        az, "account", "get-access-token",
        "--resource", resource, "--output", "json",
    ]
    if tenant:
        args += ["--tenant", tenant]
    try:
        import subprocess

        # Blocking subprocess in a worker thread so this works on any event loop
        # (the Windows SelectorEventLoop can't spawn asyncio subprocesses).
        result = await asyncio.to_thread(
            subprocess.run, args, capture_output=True, timeout=40
        )
        out, err = result.stdout, result.stderr
    except Exception as e:  # noqa: BLE001
        return None, f"az get-access-token error: {e}"
    if result.returncode != 0:
        return None, (err.decode("utf-8", "ignore") or "az login required")[:300]
    try:
        return json.loads(out.decode("utf-8", "ignore")).get("accessToken"), None
    except json.JSONDecodeError:
        return None, "Could not parse az token output."


def _has_managed_identity() -> bool:
    """True when the platform exposes a managed-identity token endpoint.

    Azure Container Apps and App Service set ``IDENTITY_ENDPOINT`` (+ ``IDENTITY_HEADER``)
    when a managed identity is assigned. We gate on this so local dev doesn't pay an IMDS
    timeout.
    """
    return bool(os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"))


def _managed_identity_client_id(conn: dict[str, Any]) -> str:
    """Client id of the user-assigned identity to use, if any.

    System-assigned identities need no id. For user-assigned, prefer the connection's
    explicit client id, then fall back to the standard ``AZURE_CLIENT_ID`` env var the
    deployment sets to select the identity.
    """
    return conn.get("client_id") or os.environ.get("AZURE_CLIENT_ID", "")


async def _managed_identity_token(
    client_id: str = "", resource: str = _ARM_RESOURCE
) -> tuple[str | None, str | None]:
    """Acquire a token from the platform managed identity.

    Uses the App Service / Container Apps identity endpoint (``IDENTITY_ENDPOINT`` +
    ``IDENTITY_HEADER``) when present — this is the only option in Container Apps, which
    does not expose IMDS — and otherwise falls back to the IMDS endpoint (VMs / AKS).
    """
    endpoint = os.environ.get("IDENTITY_ENDPOINT")
    header = os.environ.get("IDENTITY_HEADER") or os.environ.get("MSI_SECRET")
    try:
        if endpoint and header:
            params = {"resource": resource, "api-version": "2019-08-01"}
            if client_id:
                params["client_id"] = client_id
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    endpoint, params=params, headers={"X-IDENTITY-HEADER": header}
                )
            if resp.status_code != 200:
                detail = (resp.text or "")[:300]
                return None, f"Managed identity token failed ({resp.status_code}): {detail}"
            return resp.json().get("access_token"), None
        # IMDS fallback (VMs / AKS). Not available in Azure Container Apps.
        params = {"resource": resource, "api-version": "2018-02-01"}
        if client_id:
            params["client_id"] = client_id
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "http://169.254.169.254/metadata/identity/oauth2/token",
                params=params,
                headers={"Metadata": "true"},
            )
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            return None, f"IMDS token failed ({resp.status_code}): {detail}"
        return resp.json().get("access_token"), None
    except httpx.HTTPError as e:  # noqa: BLE001
        return None, f"Managed identity token error: {e}"


async def get_arm_token(conn: dict[str, Any]) -> tuple[str | None, str | None]:
    """Acquire an ARM (management.azure.com) access token for this connection.

    Returns (token, error). Used for cross-tenant discovery, scoping, and health tests.
    """
    method = conn.get("auth_method", "")
    tenant = conn.get("tenant_id", "")
    if method == "service_principal":
        if not (tenant and conn.get("client_id") and conn.get("client_secret")):
            return None, "Missing tenant id, client id or client secret."
        return await _sp_secret_token(tenant, conn["client_id"], conn["client_secret"])
    if method == "service_principal_cert":
        if not (tenant and conn.get("client_id") and conn.get("certificate_pem")):
            return None, "Missing tenant id, client id or certificate."
        return await _sp_cert_token(tenant, conn["client_id"], conn["certificate_pem"])
    if method == "az_cli_token":
        token = conn.get("access_token", "")
        if not token:
            return None, "No pasted token stored."
        if _token_expired(conn.get("token_expires_on", "")):
            return None, "Pasted token has expired — paste a fresh one."
        return token, None
    # default_chain ("Host identity"): use the platform managed identity in the cloud
    # (Container Apps / App Service / VM), and fall back to the host az CLI for local dev.
    if _has_managed_identity():
        return await _managed_identity_token(_managed_identity_client_id(conn))
    return await _cli_token(tenant)


async def get_graph_token(conn: dict[str, Any]) -> tuple[str | None, str | None]:
    """Acquire a Microsoft Graph (graph.microsoft.com) access token for this connection.

    Returns (token, error). Used by the tenant-identity assessment controls (Entra policy)
    that live in Microsoft Graph, not Azure Resource Manager. Mirrors ``get_arm_token``'s
    auth-method dispatch but requests the Graph scope/resource. The pasted-ARM-token method
    (``az_cli_token``) can't mint a Graph token, so those controls fail-closed (``error``,
    excluded from the score) rather than reporting a misleading pass.
    """
    method = conn.get("auth_method", "")
    tenant = conn.get("tenant_id", "")
    if method == "service_principal":
        if not (tenant and conn.get("client_id") and conn.get("client_secret")):
            return None, "Missing tenant id, client id or client secret."
        return await _sp_secret_token(tenant, conn["client_id"], conn["client_secret"], _GRAPH_SCOPE)
    if method == "service_principal_cert":
        if not (tenant and conn.get("client_id") and conn.get("certificate_pem")):
            return None, "Missing tenant id, client id or certificate."
        return await _sp_cert_token(tenant, conn["client_id"], conn["certificate_pem"], _GRAPH_SCOPE)
    if method == "az_cli_token":
        # An ARM token can't be used against Graph. If the operator pasted a separate Microsoft
        # Graph token (az account get-access-token --resource-type ms-graph), use it so principal
        # / group / Entra name resolution works for pasted-token connections.
        gtok = conn.get("graph_access_token", "")
        if gtok and not _token_expired(conn.get("graph_token_expires_on", "")):
            return gtok, None
        if gtok:
            return None, "Pasted Microsoft Graph token has expired — paste a fresh one (az account get-access-token --resource-type ms-graph)."
        return None, (
            "Pasted-token connection has no Microsoft Graph token. Paste one "
            "(az account get-access-token --resource-type ms-graph) so principal names resolve, "
            "or use a service-principal / managed-identity connection with Directory.Read.All."
        )
    # default_chain ("Host identity"): platform managed identity in the cloud, host az CLI locally.
    if _has_managed_identity():
        return await _managed_identity_token(_managed_identity_client_id(conn), _GRAPH_RESOURCE)
    return await _cli_token(tenant, _GRAPH_RESOURCE)

