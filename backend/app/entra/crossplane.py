"""Entra power joined to Azure RBAC power, one row per principal.

Lives here rather than in the endpoint because the export needs the same rows. It previously
did not have them: the export read all nine fields straight off ``_azure_link['principals']``,
which carries only the Azure half and under different names (``powerful_roles``, not
``azure_roles``; ``role_count``, not ``azure_all_roles``), and has no notion of an Entra role
at all. Seven of the nine columns exported blank on every row, and blank in a spreadsheet reads
as "none" — the reassuring answer, on the sheet whose entire purpose is to find people who are
powerful on both planes.
"""
from __future__ import annotations

from typing import Any


def rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per principal holding Entra power, with its Azure power attached.

    Driven by the Entra side, not the Azure side: iterating the link's principals instead
    would list everyone with any Azure role — thousands of managed identities — and say
    nothing about the correlation the sheet exists to show."""
    from app.entra.signal_defs.priv_pim import entra_power

    link = data.get("_azure_link") or {}
    principals = link.get("principals") or {}
    users = {str(u["id"]): u for u in (data.get("people") or {}).get("users") or []}

    out: list[dict[str, Any]] = []
    for pid, entra in entra_power(data).items():
        azure = (principals.get(pid) if isinstance(principals, dict) else None) or {}
        u = users.get(pid) or {}
        out.append({
            "principal_id": pid,
            "name": (entra.get("name") or u.get("upn") or u.get("display_name")
                     or azure.get("name") or pid),
            "kind": entra.get("kind", "user"),
            "entra_roles": entra.get("roles") or [],
            "entra_permissions": entra.get("permissions") or [],
            "azure_roles": azure.get("powerful_roles") or [],
            "azure_all_roles": azure.get("role_count", 0),
            "azure_broad_scopes": azure.get("broad_scopes") or [],
            "azure_subscriptions": azure.get("subscriptions") or [],
            "both_planes": bool(azure.get("powerful_roles")),
        })
    out.sort(key=lambda r: (not r["both_planes"], -len(r["azure_roles"]), r["name"]))
    return out
