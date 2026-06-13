"""MCP client: connects to the Azure MCP server, discovers tools, invokes them.

Uses the official MCP Python SDK over **stdio** transport, spawning the Azure MCP
server (`npx @azure/mcp`) as a child process. stdio is supported in all Azure MCP
distributions (http is Docker-image only), making it the most reliable choice for
local dev and a single Container App that bundles node.

Safety: the server is started with `--read-only` by default so only read/
investigation tools are exposed. Gated-write execution is a later phase; the
approval-gate infrastructure (classification, approvals) is already in place.
"""
from __future__ import annotations

import contextlib
import os
import re as _re
import time
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp import types as mcp_types

from app.agent.provider import ToolSpec

_READ_VERBS = ("list", "get", "show", "describe", "query", "read", "check", "diagnose")

# Short-lived cache of the discovered tool catalog, keyed by spawn config. Listing
# tools spawns a fresh `npx @azure/mcp` process, so caching avoids that cost on every
# turn. The catalog is static for a given server version + read-only flag, so a long TTL
# is safe and keeps an idle chat from re-paying the node cold-start on its next message.
_CATALOG_TTL_SECONDS = 3600.0
_catalog_cache: dict[tuple, tuple[float, list]] = {}


def _catalog_cache_get(key: tuple) -> list | None:
    entry = _catalog_cache.get(key)
    if entry is None:
        return None
    ts, tools = entry
    if (time.monotonic() - ts) > _CATALOG_TTL_SECONDS:
        _catalog_cache.pop(key, None)
        return None
    return tools


def _catalog_cache_set(key: tuple, tools: list) -> None:
    _catalog_cache[key] = (time.monotonic(), tools)
    # Bound the cache so distinct tenants/configs can't grow it without limit.
    if len(_catalog_cache) > 32:
        oldest = min(_catalog_cache.items(), key=lambda kv: kv[1][0])[0]
        _catalog_cache.pop(oldest, None)


def classify_tool(name: str) -> str:
    """Return 'read' or 'write' for a tool name. Defaults to 'write' when ambiguous
    to stay safe (gated). When the server runs read-only, only read tools exist."""
    lowered = name.lower()
    if any(v in lowered for v in _READ_VERBS):
        return "read"
    return "write"


# The EntraID MCP server exposes one tool per operation with verb-prefixed names
# (search_users, list_applications, create_group, delete_application, …). Its naming
# conventions differ from the Azure MCP's namespace tools, so it gets its own
# read/write classifier with the right verb sets.
_ENTRA_READ_VERBS = (
    "search", "find", "get", "list", "show", "describe", "query", "read", "check",
    "suggest", "lookup", "view", "validate",
)
_ENTRA_WRITE_VERBS = (
    "create", "update", "delete", "set", "remove", "add", "reset", "generate",
    "assign", "revoke", "enable", "disable", "rotate",
)


def classify_entra_tool(name: str) -> str:
    """Classify an EntraID MCP tool by its verb prefix. Reads run freely; anything that
    mutates the directory (create/update/delete/reset/…) is gated. Defaults to 'write'
    when ambiguous so an unrecognized tool is never run without approval."""
    tokens = set(_re.split(r"[^a-z0-9]+", name.lower()))
    if tokens & set(_ENTRA_WRITE_VERBS):
        return "write"
    if tokens & set(_ENTRA_READ_VERBS):
        return "read"
    return "write"


# Verbs that indicate a mutating operation, checked against a call's command/intent
# argument (the Azure MCP exposes namespace tools where the real operation is an arg).
_WRITE_VERBS = (
    "create", "delete", "update", "set", "remove", "add", "write", "put", "patch",
    "deploy", "restart", "start", "stop", "scale", "enable", "disable", "rotate",
    "reset", "purge", "assign", "unassign", "grant", "revoke", "import", "publish",
    "approve", "reject", "move", "rename", "regenerate", "provision", "deprovision",
    "attach", "detach", "register", "unregister", "install", "uninstall", "apply",
)


def classify_call(name: str, arguments: dict[str, Any] | None) -> str:
    """Classify a specific tool CALL as 'read' or 'write'.

    The Azure MCP server exposes one tool per service namespace (e.g. ``sql``, ``role``)
    and the actual operation is carried in the call's ``command``/``intent`` argument.
    So a name-only check is too coarse: inspect the command argument for write verbs and
    only treat the call as a write when it clearly mutates. Falls back to the name-based
    classification when there's no command argument to inspect."""
    args = arguments or {}
    op = " ".join(
        str(args.get(k, "")) for k in ("command", "intent", "operation", "action")
    ).lower()
    if op.strip():
        # Tokenize on any non-alphanumeric (the Azure MCP commands are underscore- and
        # hyphen-joined, e.g. ``sql_server_firewall-rule_delete``).
        tokens = set(_re.split(r"[^a-z0-9]+", op))
        if tokens & set(_WRITE_VERBS):
            return "write"
        if tokens & set(_READ_VERBS):
            return "read"
        # An operation was specified but matched no known verb — stay safe.
        return "write"
    # No command argument: fall back to the tool-name heuristic.
    return classify_tool(name)


@dataclass
class DiscoveredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: str  # read | write


async def _consent_elicitation_callback(context: Any, params: Any):
    """Respond to the Azure MCP server's consent prompt for destructive operations.

    Newer Azure MCP servers require MCP "elicitation": before running a destructive
    command they ask the client to confirm. If the client doesn't handle it, the server
    rejects the operation ("client does not support elicitation"). This app governs write
    safety with its OWN policy (read-only toggle + auto-execute/approval gate) BEFORE the
    call ever reaches the server, so by the time a write executes it is already permitted —
    here we simply grant consent and affirmatively fill any requested confirmation fields.
    """
    content: dict[str, Any] = {}
    try:
        schema = getattr(params, "requestedSchema", None) or {}
        props = (schema or {}).get("properties") or {}
        for key, spec in props.items():
            typ = (spec or {}).get("type")
            enum = (spec or {}).get("enum")
            if typ == "boolean":
                content[key] = True
            elif enum:
                # Prefer an affirmative enum value if present, else the first option.
                affirmative = next(
                    (
                        v
                        for v in enum
                        if str(v).lower() in ("yes", "accept", "confirm", "true", "approve", "allow")
                    ),
                    enum[0],
                )
                content[key] = affirmative
            elif typ in ("number", "integer"):
                content[key] = 1
            else:
                content[key] = "yes"
    except Exception:  # noqa: BLE001 - best-effort; still accept below
        content = {}
    return mcp_types.ElicitResult(action="accept", content=content or None)


class MCPClient:
    """Spawns the Azure MCP server over stdio per operation. Simple and robust for
    local dev; a pooled long-lived session can replace this later for performance."""

    def __init__(
        self,
        command: str,
        args: list[str],
        read_only: bool = True,
        subscription_id: str | None = None,
        token_credentials: str | None = None,
        env_overrides: dict[str, str] | None = None,
        cleanup_paths: list[str] | None = None,
        classifier: Any = None,
        env_clear: list[str] | None = None,
    ) -> None:
        full_args = list(args)
        if read_only and "--read-only" not in full_args:
            full_args.append("--read-only")
        self._read_only = read_only
        # How to classify a discovered tool as read/write when the server is NOT running
        # in a guaranteed read-only mode. Defaults to the Azure namespace-tool heuristic;
        # the EntraID client passes its own verb-prefix classifier.
        self._classifier = classifier or classify_tool
        self._cleanup_paths = list(cleanup_paths or [])
        env = dict(os.environ)
        # Strip inherited credential env vars FIRST so a spawned server can never
        # authenticate with a leaked/ambient identity — only what this connection
        # explicitly provides below is used.
        for key in (env_clear or []):
            env.pop(key, None)
        if subscription_id:
            env["AZURE_SUBSCRIPTION_ID"] = subscription_id
        if token_credentials:
            # Pin DefaultAzureCredential to a specific credential (e.g. the Azure
            # CLI) so it matches the user's `az login` instead of a broker/MI.
            env["AZURE_TOKEN_CREDENTIALS"] = token_credentials
        # Per-connection overrides (tenant id, SP creds, cert path, etc.) win last so a
        # selected Azure connection fully determines the identity for this session.
        if env_overrides:
            for k, v in env_overrides.items():
                if v:
                    env[k] = v
        self._params = StdioServerParameters(command=command, args=full_args, env=env)

    def _cleanup(self) -> None:
        for path in self._cleanup_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._cleanup_paths = []

    def _catalog_cache_key(self) -> tuple:
        """Identity for the tool-catalog cache: command + args + the tenant the
        session is bound to (different tenants may expose different tools/policy)."""
        p = self._params
        tenant = (p.env or {}).get("AZURE_TENANT_ID", "")
        return (p.command, tuple(p.args), self._read_only, tenant)

    def close(self) -> None:
        """Release per-client resources (e.g. a temp service-principal cert file).

        Safe to call multiple times; a no-op when there's nothing to clean up. The
        connection's identity files must outlive every spawned MCP child process, so
        cleanup happens once when the owning turn/operation is finished."""
        self._cleanup()

    def __del__(self) -> None:  # best-effort safety net
        try:
            self._cleanup()
        except Exception:  # noqa: BLE001 - never raise from a destructor
            pass


    @contextlib.asynccontextmanager
    async def _session(self):
        async with stdio_client(self._params) as (read, write):
            # Provide an elicitation callback so the Azure MCP server will run
            # destructive operations (it otherwise rejects them when the client can't
            # confirm consent). Our own write policy gates writes before this point.
            async with ClientSession(
                read, write, elicitation_callback=_consent_elicitation_callback
            ) as session:
                await session.initialize()
                yield session

    async def list_tools(self) -> list[DiscoveredTool]:
        # The tool catalog rarely changes within a session but listing it spawns a
        # fresh `npx @azure/mcp` process (node startup + package resolve). Cache it
        # briefly per spawn-config so back-to-back turns don't pay that cost each time.
        cache_key = self._catalog_cache_key()
        cached = _catalog_cache_get(cache_key)
        if cached is not None:
            return cached
        async with self._session() as session:
            resp = await session.list_tools()
            tools: list[DiscoveredTool] = []
            for t in resp.tools:
                # When the server runs --read-only, every exposed tool is guaranteed
                # mutation-free, so it is safe to execute without the approval gate.
                # Otherwise fall back to name-based classification (Phase 3 will use
                # per-operation tool metadata for precise gating).
                kind = "read" if self._read_only else self._classifier(t.name)
                tools.append(
                    DiscoveredTool(
                        name=t.name,
                        description=t.description or "",
                        parameters=t.inputSchema or {"type": "object", "properties": {}},
                        kind=kind,
                    )
                )
            _catalog_cache_set(cache_key, tools)
            return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._session() as session:
            result = await session.call_tool(name, arguments)
            content: list[Any] = []
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    content.append(block.text)
                else:
                    dump = getattr(block, "model_dump", None)
                    content.append(dump() if dump else str(block))
            return {"isError": result.isError, "content": content}

    @staticmethod
    def to_tool_specs(tools: list[DiscoveredTool]) -> list[ToolSpec]:
        return [
            ToolSpec(name=t.name, description=t.description, parameters=t.parameters)
            for t in tools
        ]


def build_mcp_client(settings, connection: dict[str, Any] | None = None) -> "MCPClient":
    """Construct an MCPClient from application settings and an optional Azure connection.

    The read-only flag prefers the connection's own setting, then the runtime app
    setting (admin dashboard toggle), falling back to the MCP_READ_ONLY env default.
    When a connection is supplied, its identity (tenant/service-principal/etc.) is
    injected into the spawned MCP server's environment so the session is bound to that
    tenant."""
    try:
        from app.core.app_settings import load_settings

        global_read_only = bool(load_settings().get("mcp_read_only", settings.mcp_read_only))
    except Exception:  # noqa: BLE001 - fall back to env setting
        global_read_only = settings.mcp_read_only

    env_overrides: dict[str, str] | None = None
    cleanup_paths: list[str] | None = None
    subscription_id = settings.azure_subscription_id or None
    token_credentials = settings.azure_token_credentials or None
    read_only = global_read_only

    if connection:
        from app.azure.credentials import build_mcp_env

        # A connection's own read-only flag governs its tenant (governance per tenant).
        read_only = bool(connection.get("read_only", global_read_only))
        env_overrides, cleanup_paths = build_mcp_env(connection)
        # Connection env fully specifies the credential; don't also pin the host one.
        token_credentials = None
        subscription_id = connection.get("default_subscription") or subscription_id

    return MCPClient(
        command=settings.mcp_command,
        args=settings.mcp_args.split(),
        read_only=read_only,
        subscription_id=subscription_id,
        token_credentials=token_credentials,
        env_overrides=env_overrides,
        cleanup_paths=cleanup_paths,
    )


def _entra_env_from_connection(connection: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """Map an Azure connection's service-principal identity to the env vars the EntraID
    (Microsoft Graph) MCP server expects (TENANT_ID / CLIENT_ID / CLIENT_SECRET, or a
    certificate file). Returns (env_overrides, cleanup_paths)."""
    env: dict[str, str] = {}
    cleanup: list[str] = []
    if not connection:
        return env, cleanup
    tenant = connection.get("tenant_id", "")
    client_id = connection.get("client_id", "")
    if tenant:
        env["TENANT_ID"] = tenant
    if client_id:
        env["CLIENT_ID"] = client_id
    secret = connection.get("client_secret", "")
    cert_pem = connection.get("certificate_pem", "")
    if secret:
        env["CLIENT_SECRET"] = secret
    elif cert_pem:
        # The EntraID server reads a certificate FILE path; materialize the PEM to a
        # short-lived temp file and clean it up when the owning turn finishes.
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".pem", prefix="entra_cert_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cert_pem)
        except OSError:
            pass
        env["CERTIFICATE_PATH"] = path
        cleanup.append(path)
    return env, cleanup


def build_entra_mcp_client(settings, connection: dict[str, Any] | None = None) -> "MCPClient":
    """Construct an MCPClient bound to the vendored EntraID (Microsoft Graph) MCP server.

    The server is a Python FastMCP process spawned over stdio (its heavy msgraph-sdk
    deps live in a dedicated venv). It authenticates to Microsoft Graph using the
    selected Azure connection's service-principal identity. The server has no
    ``--read-only`` switch, so write safety is governed by the app's own policy via the
    EntraID verb-prefix classifier (``classify_entra_tool``)."""
    env_overrides, cleanup_paths = _entra_env_from_connection(connection)
    import shlex

    raw_args = settings.entra_mcp_args
    args = shlex.split(raw_args, posix=False) if isinstance(raw_args, str) else list(raw_args)
    return MCPClient(
        command=settings.entra_mcp_command,
        args=args,
        read_only=False,  # FastMCP server has no --read-only flag; classify per-tool instead
        subscription_id=None,
        token_credentials=None,
        env_overrides=env_overrides,
        cleanup_paths=cleanup_paths,
        classifier=classify_entra_tool,
        # Never let the spawned Graph server inherit ambient credentials — its identity
        # must come ONLY from the selected connection (set in env_overrides above).
        env_clear=[
            "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
            "CERTIFICATE_PATH", "CERTIFICATE_PWD",
        ],
    )


async def warm_tool_catalog() -> None:
    """Pre-spawn the Azure MCP server once (per Azure connection) at startup so the
    tool catalog is cached BEFORE the user's first chat message.

    The first ``list_tools()`` of a session spawns ``npx @azure/mcp`` (node startup +
    package resolve), which adds several seconds of dead time to the first assistant
    turn — and the orchestrator awaits it before streaming any token. Warming the
    catalog here moves that one-time cost to server boot (in the background) so the
    first real message starts streaming immediately. Best-effort: never raises.
    """
    import logging

    from app.core.azure_connections import list_connections, resolve_connection
    from app.core.config import get_settings

    log = logging.getLogger("app.mcp.client")
    settings = get_settings()

    # Warm the default/host connection plus every configured connection, so the first
    # message to ANY tenant hits a warm cache (the cache key is per-tenant).
    connections: list[dict[str, Any] | None] = [resolve_connection(None)]
    try:
        for c in list_connections():
            connections.append(resolve_connection(c.get("id")))
    except Exception:  # noqa: BLE001
        pass

    seen: set[tuple] = set()
    for conn in connections:
        client = build_mcp_client(settings, connection=conn)
        try:
            key = client._catalog_cache_key()
            if key in seen:
                continue
            seen.add(key)
            tools = await client.list_tools()
            log.info("Warmed MCP tool catalog (%d tools) for tenant %s", len(tools), key[-1] or "default")
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            log.info("MCP catalog warmup skipped: %s", exc)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

