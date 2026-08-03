"""Measure the Access Map projection against a real cached tenant. Ad-hoc, not a test."""
from __future__ import annotations

import json
import sys
import time

from app.iam import compose, flow


def _richest() -> str:
    """Pick the cached tenant with the most IAM rows."""
    import app.core.azure_connections as ac

    best, best_n = "", -1
    for c in ac.list_connections():
        tid = str(c.get("tenant_id") or "")
        if not tid:
            continue
        try:
            n = len(compose.build_master_rows(tid))
        except Exception:  # noqa: BLE001 - probe only
            n = 0
        if n > best_n:
            best, best_n = tid, n
    if not best:
        raise SystemExit("no cached IAM tenant found")
    return best


def main() -> None:
    tenant = sys.argv[1] if len(sys.argv) > 1 else _richest()
    t = time.perf_counter()
    rows = compose.build_master_rows(tenant)
    build_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    result = flow.build_facts(rows)
    project_ms = (time.perf_counter() - t) * 1000

    payload = json.dumps(result)
    encoded = json.dumps(flow.encode(result))
    totals = result["totals"]
    print(f"tenant {tenant}")
    print(f"  master rows            {totals['rows']:>8,}   ({build_ms:.0f} ms)")
    print(f"  distinct facts         {totals['facts']:>8,}   ({project_ms:.0f} ms)")
    print(f"  grants                 {totals['grants']:>8,}")
    print(f"  eligible rows          {totals['eligible_rows']:>8,}")
    print(f"  deny rows              {totals['deny_rows']:>8,}")
    print(f"  group rows folded      {totals['group_rows_folded']:>8,}")
    print(f"  unexpanded groups      {totals['unexpanded_groups']:>8,}")
    print(f"  payload (objects)      {len(payload) / 1024:>8,.0f} KiB")
    print(f"  payload (interned)     {len(encoded) / 1024:>8,.0f} KiB"
          f"   ({len(payload) / max(len(encoded), 1):.1f}x smaller)")
    print(f"  truncated              {result['truncated']}")
    for n in result["notes"]:
        print(f"  note: {n}")


if __name__ == "__main__":
    main()
