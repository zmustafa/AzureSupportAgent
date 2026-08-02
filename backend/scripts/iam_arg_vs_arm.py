"""Cross-check the Resource Graph sweep against the per-scope ARM collectors on a live tenant.

The ARG pivot is only safe if both paths see the same access. This runs them side by side and
diffs the assignment-id sets per subscription. Any difference is printed rather than summarised:
a pivot that loses grants is worse than a slow one, and "the counts matched" is not the same as
"the same rows were returned".

    .venv\\Scripts\\python.exe scripts\\iam_arg_vs_arm.py <connection_id>
"""
from __future__ import annotations

import asyncio
import sys

from app.iam import arg, collectors, schema


async def main() -> int:
    connection_id = sys.argv[1] if len(sys.argv) > 1 else None
    from app.azure.arm import list_subscriptions
    from app.azure.credentials import get_arm_token
    from app.core.azure_connections import resolve_connection

    connection = resolve_connection(connection_id)
    if not connection:
        print("no connection resolved")
        return 1
    tenant_id = connection.get("tenant_id") or "default"

    token, terr = await get_arm_token(connection)
    if not token:
        print(f"no ARM token: {terr}")
        return 1

    subs, serr = await list_subscriptions(token)
    if serr:
        print(f"subscription listing: {serr}")
    names = {str(s["id"]): str(s.get("name", s["id"])) for s in subs}
    print(f"{len(subs)} subscription(s)\n")

    print("ARG sweep…")
    from app.iam.orchestrator import collect_bulk

    bulk = await collect_bulk(
        tenant_id, connection, arm_token=token,
        role_def_scope=f"/subscriptions/{subs[0]['id']}" if subs else "",
        subscription_names=names,
    )
    for st in bulk.statuses:
        print(f"  {st.collector:28} {st.status:20} {st.message[:90]}")
    print(f"  usable={bulk.usable}  role_defs={len(bulk.role_defs)}")
    arg_buckets = bulk.assignments

    print("\nARM per-subscription…")
    all_arm: set[str] = set()
    all_arg: set[str] = set()
    for sub in subs:
        sub_id = str(sub["id"])
        scope = f"/subscriptions/{sub_id}"
        arm_defs, _ = await collectors.collect_role_definitions(token, scope)
        arm_rows, arm_st = await collectors.collect_azure_rbac(
            token, scope=scope, subscription_id=sub_id, subscription_name=names[sub_id],
            tenant_id=tenant_id, role_defs=arm_defs,
        )
        arm_ids = {str(r["assignmentId"]).lower() for r in arm_rows}
        arg_ids = {str(r["assignmentId"]).lower() for r in arg_buckets.get(scope, [])}
        all_arm |= arm_ids
        all_arg |= arg_ids
        only_arm = arm_ids - arg_ids
        flag = "OK  " if not only_arm else "diff"
        print(f"  {flag} {names[sub_id][:32]:32} arm={len(arm_ids):4} arg={len(arg_ids):4} [{arm_st.status}]")

    # Per-subscription differences are EXPECTED and not a loss: ARM returns MG- and
    # tenant-root-scoped grants as inherited copies under every child subscription, while ARG
    # indexes them at their own scope. What matters is whether the MG walk picks them up, so
    # compare the UNION across everything the orchestrator actually collects.
    print("\nunion check (what the orchestrator really writes):")
    mg_ids: set[str] = set()
    from app.azure.arm import list_all_management_groups

    mgs, _mgerr = await list_all_management_groups(token)
    for mg in mgs:
        mg_scope = f"/providers/Microsoft.Management/managementGroups/{mg['id']}"
        mg_defs, _ = await collectors.collect_role_definitions(token, mg_scope)
        mg_rows, _ = await collectors.collect_azure_rbac(
            token, scope=mg_scope, subscription_id="", subscription_name=mg.get("name", ""),
            tenant_id=tenant_id, role_defs=mg_defs, collector="ManagementGroupRbac",
        )
        mg_ids |= {str(r["assignmentId"]).lower() for r in mg_rows}
    print(f"  {len(mgs)} management group(s) contribute {len(mg_ids)} assignment(s)")

    covered = all_arg | mg_ids
    lost = all_arm - covered
    print(f"  ARM total (with inherited duplicates collapsed): {len(all_arm)}")
    print(f"  ARG subscriptions + ARM management groups:       {len(covered)}")
    if lost:
        print(f"  LOST {len(lost)} assignment(s) no collected scope returns:")
        for a in sorted(lost)[:10]:
            print(f"    {a}")
    else:
        print("  no assignment is lost by the pivot")

    print("\nrole naming:")
    from app.iam.orchestrator import _is_bare_guid

    unnamed = [r for rows in arg_buckets.values() for r in rows if _is_bare_guid(r["roleName"])]
    total = sum(len(v) for v in arg_buckets.values())
    print(f"  {len(unnamed)} of {total} ARG row(s) fell back to a bare role GUID")
    priv_arg = sum(1 for rows in arg_buckets.values() for r in rows if r["roleIsPrivileged"])
    print(f"  privileged rows seen by ARG: {priv_arg}")

    return 0 if not lost else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
