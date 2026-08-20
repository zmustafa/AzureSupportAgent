"""Local sanity check for the Entra demo snapshot (score, pillars, CA analysis).

Run: backend\\.venv\\Scripts\\python.exe backend\\scripts\\entra_demo_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entra import azure_link, ca_simulator, demo, snapshot as sm  # noqa: E402
from app.entra.collectors import pim as pim_mod  # noqa: E402


def privileged_section(data: dict, a: dict) -> None:
    print("\n=== PRIVILEGED ACCESS (P3) ===")
    pim = data.get("pim") or {}
    roles = data.get("roles") or {}
    policies = pim_mod.privileged_policies(pim, roles)
    print(f"  {len(policies)} PIM policy row(s) for privileged roles")
    for p in policies[:5]:
        print(f"    {p['role_name']:32} score={p['score']:3} "
              f"missing={','.join(p['failed_controls']) or '-'}")
    acts = pim.get("activations") or []
    print(f"  {len(acts)} activation request(s) in the lookback window")

    link = data.get("_azure_link") or azure_link.empty("no analysis")
    print(f"  Azure link available={link['available']} stale={link.get('stale')} "
          f"reason={link.get('reason') or '-'}")


def apps_section(data: dict) -> None:
    print("\n=== APPLICATION 360 (P4) ===")
    apps = data.get("apps") or {}
    sps = apps.get("service_principals") or []
    scored = sorted(sps, key=lambda s: -((s.get("risk") or {}).get("score") or 0))
    print(f"  {len(sps)} service principal(s); top by risk:")
    for s in scored[:6]:
        r = s.get("risk") or {}
        top = ", ".join(f"{c['key']}={c['points']}" for c in (r.get("components") or [])[:3])
        print(f"    {(s.get('display_name') or '?')[:34]:34} {r.get('score', 0):3}  {top}")
    grants = sum(1 for s in sps for g in (s.get("granted_delegated") or [])
                 if g.get("consent_type") == "AllPrincipals")
    app_roles = sum(len(s.get("granted_application") or []) for s in sps)
    print(f"  {grants} tenant-wide delegated grant(s), {app_roles} application permission(s) granted")


def simulator_section(data: dict, a: dict) -> None:
    print("\n=== CA SIMULATOR (P5) ===")
    ca = a["ca"]
    policies = ca.get("policies") or []
    disabled = [p for p in policies if not p["is_enforced"]]
    enabled = [p for p in policies if p["is_enforced"]]
    for label, changes in (
        ("enable a report-only/disabled policy",
         [{"kind": "enable", "policy_id": disabled[0]["id"]}] if disabled else []),
        ("disable an enforced policy",
         [{"kind": "disable", "policy_id": enabled[0]["id"]}] if enabled else []),
    ):
        if not changes:
            print(f"  (skipped: {label} — no candidate policy)")
            continue
        res = ca_simulator.simulate(data, ca, changes)
        c = res["counts"]
        print(f"  {label}:")
        print(f"    blocked={c['newly_blocked']} challenged={c['newly_challenged']} "
              f"granted={c['newly_granted']} protection_lost={c['protection_lost']} "
              f"unchanged={c['unchanged']}")
        print(f"    confidence={res['confidence_label']} "
              f"break-glass impacted={len(res['break_glass_impact'])} "
              f"sampled {res['sampling']['evaluated']}/{res['sampling']['total_principals']}")
        for case in res["cases"][:3]:
            print(f"      {case['principal'][:28]:28} {case['context_label'][:22]:22} "
                  f"{case['from']} -> {case['to']}")


def main() -> None:
    demo.seed()
    snap = sm.analyze(demo.DEMO_TENANT, force=True)
    a = snap["_analysis"]
    s = a["score"]
    print(f"score={s['score']} grade={s['grade']!r} coverage={s['coverage']:.0%} "
          f"findings={len(a['findings'])}")
    print()
    for p in s["pillars"]:
        print(f"  {p['key']:5} {str(p['score']):>5} {p['state']:13} "
              f"findings={p['findings']:3} measured={p['measured_signals']}/{p['total_signals']}")
    print("\nTOP WINS")
    for w in s["top_wins"][:6]:
        print(f"  +{w['points']:.1f}  {w['signal_id']:40} x{w['findings']}")

    ca = a["ca"]
    print("\nCA counts:", ca["counts"])
    h = ca["coverage"]["headline"]
    print("headline: uncovered_users=%s uncovered_apps=%s privileged_uncovered=%s"
          % (h["uncovered_users"], h["uncovered_apps"], h["privileged_uncovered"]))
    print("conflicts:", [(c["kind"], c["policy_name"]) for c in ca["conflicts"]])
    print("break-glass:", [(c["upn"], c["score"], c["confirmed"], c["lockout_risk"])
                           for c in ca["breakglass"]["candidates"]])

    print("\nNOT MEASURED (first 8)")
    for k, v in list(a["not_measured"].items())[:8]:
        print(f"  {k:40} {v}")
    print("\nsignal errors:", a["errors"])

    print("\nFINDINGS BY SIGNAL")
    for sid, n in sorted(a["by_signal"].items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {sid:42} {n}")

    privileged_section(snap["data"], a)
    apps_section(snap["data"])
    simulator_section(snap["data"], a)


if __name__ == "__main__":
    main()
