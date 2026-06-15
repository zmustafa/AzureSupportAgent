"""Read-only RBAC agent tools — answer access questions from the cached scan.

Exposes the access-review data to the LLM through the same ``ConnectorTool`` shape the other
built-in tools use, so the orchestrator's tool loop dispatches them uniformly. All three are
strictly READ-ONLY (no approval pause): they query the per-scope cache via :mod:`compose`, never
Azure directly, so they're instant and side-effect free. Gated by the ``rbac_tools_enabled``
admin setting."""
from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.rbac import compose, schema


def _fmt_rows(rows: list[dict[str, Any]], *, limit: int = 50) -> str:
    """Compact, model-friendly rendering of access rows."""
    lines: list[str] = []
    for r in rows[:limit]:
        who = r.get("effectivePrincipalName") or r.get("principalDisplayName") or r.get("effectivePrincipalId") or "(unknown)"
        path = r.get("accessPath", "")
        via = f" via {r.get('sourceGroupName')}" if path == schema.PATH_GROUP else (" (owner)" if path == schema.PATH_OWNER else "")
        scope = r.get("scopeDisplayName") or r.get("subscriptionName") or r.get("scope") or "directory"
        flag = " ⚠privileged" if r.get("roleIsPrivileged") else ""
        lines.append(f"- {who}: {r.get('roleName','')} @ {scope}{via}{flag} [{r.get('surface','')}]")
    more = f"\n…and {len(rows) - limit} more." if len(rows) > limit else ""
    return ("\n".join(lines) + more) if lines else "(no matching access found)"


def _make_who_can_access(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        scope_q = str(args.get("scope") or "").strip().lower()
        privileged_only = bool(args.get("privileged_only", False))
        rows = compose.build_master_rows(tenant_id)
        if scope_q:
            rows = [
                r
                for r in rows
                if scope_q in str(r.get("scope", "")).lower()
                or scope_q in str(r.get("scopeDisplayName", "")).lower()
                or scope_q in str(r.get("subscriptionName", "")).lower()
                or scope_q in str(r.get("resourceName", "")).lower()
            ]
        if privileged_only:
            rows = [r for r in rows if r.get("roleIsPrivileged")]
        if not rows:
            return ok("No cached access matches that scope. Run an access refresh on the RBAC page first.")
        rows.sort(key=lambda r: (not r.get("roleIsPrivileged"), r.get("roleName", "")))
        return ok(f"{len(rows)} access grant(s) matching '{scope_q or 'any scope'}':\n\n{_fmt_rows(rows)}")

    return _handler


def _make_privileged_review(tenant_id: str):
    async def _handler(_config: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
        rows = [r for r in compose.build_master_rows(tenant_id) if r.get("roleIsPrivileged")]
        if not rows:
            return ok("No privileged access in the cached scan (or nothing scanned yet).")
        owners = [r for r in rows if r.get("accessPath") == schema.PATH_OWNER]
        group = [r for r in rows if r.get("accessPath") == schema.PATH_GROUP]
        principals = sorted({r.get("effectivePrincipalName") or r.get("principalDisplayName") or r.get("effectivePrincipalId") for r in rows})
        head = (
            f"{len(rows)} privileged grant(s) across {len(principals)} principal(s); "
            f"{len(group)} via group membership, {len(owners)} via service-principal ownership.\n\n"
        )
        return ok(head + _fmt_rows(rows))

    return _handler


def _make_effective_for_principal(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        q = str(args.get("principal") or "").strip().lower()
        if not q:
            return err("Provide a principal name, UPN, or id.")
        rows = [
            r
            for r in compose.build_master_rows(tenant_id)
            if q in str(r.get("effectivePrincipalName", "")).lower()
            or q in str(r.get("effectivePrincipalUserPrincipalName", "")).lower()
            or q in str(r.get("effectivePrincipalId", "")).lower()
            or q in str(r.get("principalDisplayName", "")).lower()
        ]
        if not rows:
            return ok(f"No cached access found for '{q}'.")
        return ok(f"Effective access for '{q}' ({len(rows)} grant(s)):\n\n{_fmt_rows(rows)}")

    return _handler


def build_rbac_tools(tenant_id: str) -> list[ConnectorTool]:
    """The three read-only access-review tools bound to a tenant's cached scan."""
    return [
        ConnectorTool(
            name="who_can_access",
            description=(
                "List who has access to an Azure scope (subscription, resource group, or resource) "
                "from the latest cached RBAC access scan. Includes effective access via group "
                "membership and service-principal ownership. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Scope to filter by — a subscription name/id, resource group, or resource name substring."},
                    "privileged_only": {"type": "boolean", "description": "Only return privileged (Owner/Contributor/UAA/data-owner) grants."},
                },
            },
            kind="read",
            handler=_make_who_can_access(tenant_id),
        ),
        ConnectorTool(
            name="privileged_access_review",
            description=(
                "Summarize all privileged access (Owner, Contributor, User Access Administrator, "
                "data-plane owner roles, Entra admin roles) from the latest cached RBAC scan, "
                "including access granted via groups and service-principal ownership. Read-only."
            ),
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_make_privileged_review(tenant_id),
        ),
        ConnectorTool(
            name="effective_access_for_principal",
            description=(
                "Show every Azure/Entra access grant a given user, group, or service principal "
                "effectively has, from the latest cached RBAC scan (direct + via group + as owner). "
                "Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {"principal": {"type": "string", "description": "Principal display name, UPN, or object id."}},
                "required": ["principal"],
            },
            kind="read",
            handler=_make_effective_for_principal(tenant_id),
        ),
    ]


def register_rbac_tools(toolset, *, tenant_id: str) -> None:
    """Add the RBAC tools to a connector toolset when enabled (mirrors register_profiler_tool)."""
    from app.core.app_settings import load_settings

    if not bool(load_settings().get("rbac_tools_enabled", True)):
        return
    try:
        toolset.add_connector({"tenant_id": tenant_id}, build_rbac_tools(tenant_id))
    except Exception:  # noqa: BLE001 - never let tool registration break a turn
        pass
