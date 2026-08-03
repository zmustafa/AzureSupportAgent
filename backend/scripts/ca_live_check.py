"""Run the CA analysis against a real cached tenant. Ad-hoc verification, not a test.

Usage:  python scripts/ca_live_check.py [tenant-id]

With no argument it picks the cached tenant with the most Conditional Access policies, which
is the one worth checking. No tenant identifier is hard-coded here: this file is committed to a
public repository and a live tenant GUID in source has reconnaissance value even though it is
not a secret.
"""
from __future__ import annotations

import sys
import time

from app.entra import ca_engine, ca_exposure, ca_taxonomy, cache
from app.entra import snapshot as sm


def _richest_tenant() -> str:
    best, best_count = "", -1
    for tid in cache.read_index():
        ca = (cache.read_domain(tid, "ca") or {}).get("data") or {}
        count = len(ca.get("policies") or [])
        if count > best_count:
            best, best_count = tid, count
    if not best:
        raise SystemExit("No cached Entra tenant found. Refresh a tenant first.")
    return best


TENANT = sys.argv[1] if len(sys.argv) > 1 else _richest_tenant()


def main() -> None:
    data = sm.load(TENANT)["data"]
    print(f"tenant {TENANT}")
    print(f"  policies={len(((data.get('ca') or {}).get('policies')) or [])} "
          f"sps={len(((data.get('apps') or {}).get('service_principals')) or [])} "
          f"users={len(((data.get('people') or {}).get('users')) or [])}")

    t = time.perf_counter()
    analysis = ca_engine.analyse(data, tenant_id=TENANT)
    print(f"  analyse() -> {(time.perf_counter()-t)*1000:.0f} ms")

    cov = analysis["coverage"]
    idx = cov["app_index"]
    print(f"\napp classification ({idx['app_count']} apps):")
    for cid, n in sorted(idx["members"].items(), key=lambda kv: -kv[1]):
        label = next((c["label"] for c in ca_taxonomy.classes() if c["id"] == cid), cid)
        print(f"  {label:32} {n:>5}")

    row = next(r for r in cov["matrix"] if r["cohort"] == "members")
    states: dict[str, int] = {}
    for cell in row["cells"].values():
        states[cell["state"]] = states.get(cell["state"], 0) + 1
    print(f"\ncell states (members cohort): {states}")

    from app.entra.signal_defs import ca_appclass
    wanted = {s.id for s in ca_appclass.SPECS}
    findings = [f for f in (sm.analyse(TENANT)["_analysis"].get("findings") or [])
                if f.get("signal_id") in wanted]
    exp = ca_exposure.build(cov, findings)
    print(f"\nexposure rows ({len(exp['rows'])}), findings joined: {len(findings)}")
    for r in exp["rows"]:
        print(f"  {r['worst_severity']:>8}  {r['label']:32} "
              f"controls {r['controls_covered']}/{r['controls_total']:<3} "
              f"findings {r['finding_count']}")

    print(f"\nshadowed: {exp['shadowed'].get('classes')}")
    print(f"unattributed measured: {exp['unattributed'].get('measured')}")

    print("\nnew app-class findings:")
    for f in findings[:15]:
        print(f"  [{f['severity']:>8}] {f['title']}")


if __name__ == "__main__":
    main()
