"""Read-only ownership agent tools — answer "who owns this?" from the registries.

Exposed to the LLM via the same ``ConnectorTool`` shape as the other built-in tools, so the
orchestrator's tool loop dispatches them uniformly. All three are strictly READ-ONLY (no
approval pause) — they query the local ownership registries via :mod:`app.ownership.resolve`,
never Azure, so they're instant and side-effect free. Gated by ``ownership_tools_enabled``."""
from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.ownership import registry, resolve


def _make_who_owns(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        subject_id = str(args.get("subject_id") or args.get("resource_id") or "").strip()
        subject_kind = str(args.get("subject_kind") or "resource").strip()
        if not subject_id:
            return err("Provide a subject_id (an ARM resource id, subscription, workload id, etc.).")
        res = resolve.resolve_owner(tenant_id, subject_kind, subject_id)
        if res["unowned"]:
            chain = ""
            return ok(f"'{subject_id}' is UNOWNED — no accountable owner found (no direct "
                      f"assignment, owner tag, owning workload, or owned ancestor scope).{chain}")
        lines = []
        for o in res["owners"]:
            tag = " (primary)" if o["primary"] else ""
            lines.append(f"- {o['display_name']}{tag} · {o['role']}" + (f" <{o['email']}>" if o["email"] else ""))
        via = res["source"]
        inh = res.get("inherited_from")
        via_txt = f" (via {inh['kind']} '{inh['name']}')" if inh else ""
        return ok(f"Owner of '{subject_id}' [{via}{via_txt}]:\n" + "\n".join(lines))

    return _handler


def _make_what_owns(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        q = str(args.get("owner") or "").strip().lower()
        if not q:
            return err("Provide an owner name or email.")
        owners = [
            o for o in registry.list_owners(tenant_id)
            if q in o["display_name"].lower() or q in (o.get("email") or "").lower()
        ]
        if not owners:
            return ok(f"No owner matching '{q}'.")
        out: list[str] = []
        for o in owners:
            assignments = registry.list_assignments(tenant_id, owner_id=o["id"])
            out.append(f"{o['display_name']} ({o['kind']}) — {len(assignments)} assignment(s):")
            for a in assignments[:50]:
                out.append(f"  - {a['subject_kind']}: {a.get('subject_name') or a['subject_id']} · {a['role']}")
        return ok("\n".join(out))

    return _handler


def _make_find_unowned(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        # Reports unowned workloads + architectures (the ownable "subjects" the resolver knows
        # without an Azure scan). For resource-level coverage, point the user at /ownership.
        ctx = resolve.build_context(tenant_id)
        unowned: list[str] = []
        try:
            from app.workloads.registry import list_workloads
            for wl in list_workloads():
                r = resolve.resolve_owner(tenant_id, "workload", wl["id"], ctx=ctx)
                if r["unowned"]:
                    unowned.append(f"- workload: {wl.get('name', wl['id'])}")
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.architectures.registry import list_architectures
            for arch in list_architectures(tenant_id):
                r = resolve.resolve_owner(tenant_id, "architecture", arch["id"], ctx=ctx)
                if r["unowned"]:
                    unowned.append(f"- architecture: {arch.get('name', arch['id'])}")
        except Exception:  # noqa: BLE001
            pass
        if not unowned:
            return ok("Every workload and architecture has an owner. 🎉 (Resource-level coverage: see /ownership → Coverage.)")
        return ok(f"{len(unowned)} unowned subject(s):\n" + "\n".join(unowned[:100]))

    return _handler


def build_ownership_tools(tenant_id: str) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="who_owns",
            description=(
                "Find the accountable owner of an Azure resource, subscription, resource group, "
                "workload or architecture, using the ownership directory (direct assignment, owner "
                "tag, owning workload, or inherited ancestor scope). Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "subject_id": {"type": "string", "description": "ARM resource id / subscription guid / workload id / architecture id."},
                    "subject_kind": {"type": "string", "description": "resource | subscription | resource_group | workload | architecture (default resource)."},
                },
                "required": ["subject_id"],
            },
            kind="read",
            handler=_make_who_owns(tenant_id),
        ),
        ConnectorTool(
            name="what_does_owner_own",
            description="List everything a given owner or team is assigned to (their estate). Read-only.",
            parameters={
                "type": "object",
                "properties": {"owner": {"type": "string", "description": "Owner display name or email."}},
                "required": ["owner"],
            },
            kind="read",
            handler=_make_what_owns(tenant_id),
        ),
        ConnectorTool(
            name="find_unowned",
            description="List workloads and architectures that have no accountable owner. Read-only.",
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_make_find_unowned(tenant_id),
        ),
    ]


def register_ownership_tools(toolset, *, tenant_id: str) -> None:
    """Add the ownership tools to a connector toolset when enabled (mirrors register_rbac_tools)."""
    from app.core.app_settings import load_settings

    if not bool(load_settings().get("ownership_tools_enabled", True)):
        return
    try:
        toolset.add_connector({"tenant_id": tenant_id}, build_ownership_tools(tenant_id))
    except Exception:  # noqa: BLE001 - never let tool registration break a turn
        pass
