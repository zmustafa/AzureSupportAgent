"""Graph permission review: what could this connection's credential actually do?

Plan reference: docs/improvement-plans/security-hardening/11-azure-blast-radius.md

The damage a compromised credential can do is decided by how over-privileged it is, not
by how it was compromised. Reviewing that periodically is far more valuable than trying
to make disclosure impossible.

READ-ONLY. Enumerates only; changes nothing.

    cd backend
    .venv\\Scripts\\python.exe scripts\\graph_permission_review.py <connection-id>

Reports:
  1. Every Graph application role granted, resolved to its name, classified read vs WRITE.
  2. Credentials on the app registration -- an attacker who used a leaked secret would add
     their own for persistence, so anything unexpected here means the compromise is ongoing.
  3. A blast-radius summary.

Application permissions are tenant-wide, unconditional, and NOT subject to Conditional
Access. A leaked app-only secret is therefore equivalent to everything listed here, with
no MFA and no device-compliance check in the way.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.core.azure_connections import get_connection
from app.entra.graphclient import GraphClient

# Substrings that mark a role as capable of CHANGING the directory. This product is a
# read-only reporting tool, so any hit is a finding unless a shipped feature needs it.
_WRITE_MARKERS = (".ReadWrite.", ".Write.", ".Remove", ".Create", ".Delete",
                  ".Manage", ".FullControl", ".AccessAsUser")

# Roles that grant a direct path to global-admin-equivalent control.
_CROWN_JEWELS = {
    "Directory.ReadWrite.All",
    "RoleManagement.ReadWrite.Directory",
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "PrivilegedAccess.ReadWrite.AzureAD",
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
}


def _classify(value: str) -> str:
    if value in _CROWN_JEWELS:
        return "CROWN-JEWEL"
    if any(m in value for m in _WRITE_MARKERS):
        return "WRITE"
    return "read"


async def review(connection_id: str) -> int:
    conn = get_connection(connection_id)
    if not conn:
        print(f"No such connection: {connection_id}")
        return 2

    client_id = conn.get("client_id") or ""
    label = conn.get("display_name") or connection_id
    print(f"\n{'=' * 78}")
    print(f" GRAPH PERMISSION REVIEW - connection {label!r}")
    print(f" tenant    {conn.get('tenant_id')}")
    print(f" client_id {client_id}")
    print(f"{'=' * 78}")

    async with GraphClient(conn) as gc:
        # ---- resolve our own service principal ---------------------------------
        # NOTE: GraphClient.get_all returns a (items, truncated) TUPLE, not a list.
        sps, _ = await gc.get_all(
            "/servicePrincipals",
            filter=f"appId eq '{client_id}'",
            select=["id", "displayName", "appId"],
        )
        sp = sps[0] if sps else None
        if not isinstance(sp, dict) or not sp.get("id"):
            print("Could not resolve the service principal (needs Application.Read.All).")
            return 2
        sp_id = sp["id"]
        print(f" sp object {sp_id}  ({sp.get('displayName')})\n")

        # ---- what has been granted ---------------------------------------------
        grants, _ = await gc.get_all(f"/servicePrincipals/{sp_id}/appRoleAssignments")
        grants = list(grants or [])

        # Resolve appRoleId -> human name, per resource SP (Graph, ARM, ...).
        resource_ids = {g.get("resourceId") for g in grants if g.get("resourceId")}
        catalog: dict[str, dict[str, Any]] = {}
        for rid in resource_ids:
            res = await gc.get(
                f"/servicePrincipals/{rid}",
                params={"$select": "id,displayName,appRoles"},
            )
            if isinstance(res, dict):
                for role in res.get("appRoles") or []:
                    catalog[str(role.get("id"))] = {
                        "value": role.get("value") or "",
                        "display": role.get("displayName") or "",
                        "resource": res.get("displayName") or "",
                    }

        rows = []
        for g in grants:
            meta = catalog.get(str(g.get("appRoleId")), {})
            value = meta.get("value") or f"<unresolved {g.get('appRoleId')}>"
            rows.append({
                "resource": meta.get("resource") or g.get("resourceDisplayName") or "?",
                "value": value,
                "kind": _classify(value),
                "granted": (g.get("createdDateTime") or "")[:10],
            })
        rows.sort(key=lambda r: ({"CROWN-JEWEL": 0, "WRITE": 1, "read": 2}[r["kind"]], r["value"]))

        print(f" {len(rows)} application role(s) granted\n")
        print(f" {'KIND':<12} {'RESOURCE':<22} {'ROLE':<44} GRANTED")
        print(f" {'-' * 12} {'-' * 22} {'-' * 44} {'-' * 10}")
        for r in rows:
            print(f" {r['kind']:<12} {r['resource'][:22]:<22} {r['value'][:44]:<44} {r['granted']}")

        writes = [r for r in rows if r["kind"] != "read"]
        crown = [r for r in rows if r["kind"] == "CROWN-JEWEL"]

        # ---- persistence check --------------------------------------------------
        print(f"\n{'-' * 78}\n CREDENTIALS ON THE APP REGISTRATION (persistence check)\n{'-' * 78}")
        try:
            # Two steps on purpose: resolve the object id with a minimal $select, then
            # fetch the object itself. Selecting passwordCredentials/keyCredentials
            # directly on the filtered COLLECTION returns an empty page.
            apps, _ = await gc.get_all(
                "/applications",
                filter=f"appId eq '{client_id}'",
                select=["id", "displayName"],
            )
            app = None
            if apps:
                app = await gc.get(
                    f"/applications/{apps[0]['id']}",
                    params={"$select": "id,displayName,passwordCredentials,keyCredentials"},
                )
            if isinstance(app, dict) and app.get("id"):
                pwds = app.get("passwordCredentials") or []
                keys = app.get("keyCredentials") or []
                print(f" {len(pwds)} secret(s), {len(keys)} certificate(s)")
                for c in [*pwds, *keys]:
                    print(f"   - {c.get('displayName') or '(no name)':<34} "
                          f"start={(c.get('startDateTime') or '')[:10]} "
                          f"end={(c.get('endDateTime') or '')[:10]}")
                if len(pwds) + len(keys) > 1:
                    print("\n   REVIEW EACH. An attacker who obtained a leaked secret would add")
                    print("   their OWN credential for persistence -- rotation alone would not evict")
                    print("   them. Anything you cannot account for means the compromise is ongoing.")
            else:
                print(" could not read the application object (needs Application.Read.All)")
        except Exception as exc:  # noqa: BLE001 - review tool, report and continue
            print(f" could not read application credentials: {type(exc).__name__}: {exc}")

    # ---- verdict ---------------------------------------------------------------
    print(f"\n{'=' * 78}\n BLAST RADIUS\n{'=' * 78}")
    print(f" application roles granted : {len(rows)}")
    print(f" write-capable             : {len(writes)}")
    print(f" crown-jewel (admin path)  : {len(crown)}")
    if crown:
        print("\n CRITICAL - these grant a direct path to tenant takeover:")
        for r in crown:
            print(f"   * {r['value']}")
    if writes and not crown:
        print("\n WRITE-capable roles on a READ-ONLY product. Justify each against a shipped")
        print(" feature, or remove it:")
        for r in writes:
            print(f"   * {r['value']}")
    if not writes:
        print("\n No write-capable application roles. A leaked secret for this connection")
        print(" grants READ access only -- materially survivable.")
    print()
    return 1 if crown else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(review(sys.argv[1])))
