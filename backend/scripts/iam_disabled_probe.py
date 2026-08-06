"""Phase 0 probe: how should account state actually be collected on THIS tenant?

Answers three questions with real data before anything is optimised for a guess:

1. Does ``directoryObjects/getByIds`` already return ``accountEnabled``? If it does, the whole
   feature costs **zero additional Graph calls** and reads nothing beyond the principals that
   actually hold access.
2. If not, does the inverted sweep (``/users?$filter=accountEnabled eq false``) work with the
   connection's existing consent, and how big is the disabled population? A sweep larger than
   ``collectors.MAX_DISABLED_SWEEP`` gets capped, and a capped sweep may not declare anybody
   enabled.
3. How many principals holding access are actually disabled — the number that decides whether
   the rest of the feature is worth the screen space on this tenant.

Run against a configured connection. The argument may be a connection id OR its display name
(case-insensitive); with no argument the default connection is used. Selecting by name means a
run against a particular tenant leaves no identifier behind in this file:

    python scripts/iam_disabled_probe.py [connection-id-or-name]

Read-only. Makes no writes and changes no cache.
"""
from __future__ import annotations

import asyncio
import sys

from app.core.azure_connections import list_connections, resolve_connection
from app.iam import cache, collectors, compose, schema


def pick_connection(selector: str) -> dict | None:
    """Resolve a connection by id, else by display name, else the default."""
    conn = resolve_connection(selector or None)
    if conn and (not selector or conn.get("id") == selector):
        return conn
    wanted = (selector or "").strip().lower()
    for c in list_connections():
        if str(c.get("display_name", "")).strip().lower() == wanted:
            return c
    return conn


async def main() -> int:
    selector = sys.argv[1] if len(sys.argv) > 1 else ""
    connection = pick_connection(selector)
    if not connection:
        print("No Azure connection configured — nothing to probe.")
        return 1
    tenant_id = connection.get("tenant_id") or "default"
    print(f"connection: {connection.get('display_name') or '(default)'}")

    from app.azure.credentials import get_graph_token

    token, err = await get_graph_token(connection)
    if not token:
        print(f"No Microsoft Graph token: {err}")
        return 1

    # --- which principals hold access here ------------------------------------------------
    rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    ids = sorted({
        str(r.get("effectivePrincipalId") or r.get("principalId") or "") for r in rows
    } - {""})
    print(f"principals holding access: {len(ids)}")
    if not ids:
        print("Nothing cached — run an access refresh first.")
        return 1

    # --- Q1: is accountEnabled free from getByIds? ----------------------------------------
    sample = ids[:200]
    resolved, st = await collectors.collect_principal_directory(token, sample)
    with_state = [p for p in resolved if p.get("accountEnabled") != schema.ENABLED_UNKNOWN]
    print(f"\nQ1 getByIds: {len(resolved)}/{len(sample)} resolved [{st.status}]")
    print(f"   carrying accountEnabled: {len(with_state)}")
    # Measured on a real tenant: 29 of 195. `getByIds` returns the DEFAULT property set for each
    # object type, and that set differs by type — so this is a partial answer, never a complete
    # one. Reporting "free, no extra calls" off a non-zero count would have been exactly the
    # kind of confident wrong summary this probe exists to prevent.
    if not resolved:
        print("   -> nothing resolved; the sweep is the only available source (Q2).")
    elif not with_state:
        print("   -> absent entirely. The inverted sweep is required (Q2).")
    elif len(with_state) == len(resolved):
        print("   -> FREE for every principal. No extra calls needed.")
    else:
        pct = 100 * len(with_state) / len(resolved)
        print(
            f"   -> PARTIAL ({pct:.0f}%). The remaining {len(resolved) - len(with_state)} still "
            f"need the sweep, so both paths run."
        )
        by_type: dict[str, tuple[int, int]] = {}
        for p in resolved:
            t = str(p.get("principalType") or "?")
            got, tot = by_type.get(t, (0, 0))
            by_type[t] = (got + (p.get("accountEnabled") != schema.ENABLED_UNKNOWN), tot + 1)
        for t, (got, tot) in sorted(by_type.items()):
            print(f"      {t:16} {got}/{tot} carry accountEnabled")

    # --- Q2: the inverted sweep -----------------------------------------------------------
    print("\nQ2 inverted sweep (/users?$filter=accountEnabled eq false):")
    users, uerr, ucode = await collectors._get_all(
        token,
        "https://graph.microsoft.com/v1.0/users",
        {"$filter": "accountEnabled eq false", "$select": "id", "$count": "true", "$top": "999"},
        extra_headers={"ConsistencyLevel": "eventual"},
        max_items=collectors.MAX_DISABLED_SWEEP,
    )
    if uerr:
        print(f"   FAILED (HTTP {ucode}): {uerr}")
        print("   -> the connection's consent does not cover this; every principal stays unknown.")
    else:
        capped = len(users) >= collectors.MAX_DISABLED_SWEEP
        print(f"   disabled users: {len(users)}{' (CAPPED — incomplete)' if capped else ''}")

    sps, serr, scode = await collectors._get_all(
        token,
        "https://graph.microsoft.com/v1.0/servicePrincipals",
        {"$filter": "accountEnabled eq false", "$select": "id", "$count": "true", "$top": "999"},
        extra_headers={"ConsistencyLevel": "eventual"},
        max_items=collectors.MAX_DISABLED_SWEEP,
    )
    print(
        f"   disabled service principals: {len(sps)}"
        if not serr
        else f"   servicePrincipals FAILED (HTTP {scode}): {serr}"
    )

    # --- Q3: how many of them actually hold access ----------------------------------------
    disabled_ids = {str(o.get("id") or "").lower() for o in [*users, *sps]} - {""}
    holding = [i for i in ids if i.lower() in disabled_ids]
    print(f"\nQ3 disabled principals HOLDING ACCESS: {len(holding)} of {len(ids)}")
    if holding:
        by_id = {i.lower(): i for i in holding}
        priv = {
            str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower()
            for r in rows
            if r.get("roleIsPrivileged")
        } & set(by_id)
        print(f"   of which hold a privileged role: {len(priv)}")
        grants = sum(
            1 for r in rows
            if str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() in by_id
        )
        print(f"   total grants held by disabled principals: {grants}")

    # --- what the cache currently believes ------------------------------------------------
    cached = (await asyncio.to_thread(cache.read_directory, tenant_id)).get("principal_state") or {}
    print(f"\ncached principal_state entries: {len(cached)}")
    if not cached:
        print("   -> the disabled-access report will correctly report NOT MEASURED until a")
        print("      directory refresh runs with the account-state collector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
