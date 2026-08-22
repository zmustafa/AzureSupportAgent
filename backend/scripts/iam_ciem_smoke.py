"""Live smoke for the P8 CIEM surface: usage, right-sizing and the what-if simulator.

Checks the claims this feature cannot get wrong on real data:

  1. usage reports its OWN freshness and says explicitly when it has not been measured;
  2. every over-privilege figure carries its denominator, window and confidence;
  3. break-glass principals are reported but never recommended for removal;
  4. data-plane roles are excluded while data-plane logging is unavailable, and it is stated;
  5. the simulator refuses an invalid change (400) and a deleted referent (409) rather than
     returning a reassuring empty diff;
  6. `access_retained_via_other_path` is computed, and the sample size and population are
     published at the top level.

    .venv\\Scripts\\python.exe scripts\\iam_ciem_smoke.py [connection_id] [--scan]
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:35001/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(path: str, body: dict | None = None, method: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("Sec-Fetch-Site", "same-origin")
    req.add_header("Origin", "http://127.0.0.1:35001")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=900) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400].decode(errors="replace")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_scan = "--scan" in sys.argv
    conn = args[0] if args else ""
    q = f"?connection_id={conn}" if conn else ""
    amp = "&" if q else "?"
    call("/auth/login", {"username": "admin", "password": "admin"})

    failures: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")
        if not cond:
            failures.append(label)

    # ---------------------------------------------------------------- usage
    if do_scan:
        print("running a usage scan (slow — Activity Log, per subscription)…")
        status, r = call(f"/iam/usage/refresh{q or '?'}{amp if q else ''}days=90".replace("??", "?"), {})
        print(f"  scan -> {status} {str(r)[:160]}")

    status, u = call(f"/iam/usage{q}")
    if status != 200 or not isinstance(u, dict):
        print(f"GET /iam/usage -> {status} {u}")
        return 1
    print(f"\n/iam/usage  measured={u['measured']}  window={u['window_days']}d  "
          f"events={u['event_count']}  principals={u['principal_count']}")
    print(f"  collected_at={u['generated_at'] or '(never)'}")
    check("usage reports its own freshness separately from the access snapshot", "generated_at" in u)
    check("usage says explicitly whether it was measured", isinstance(u["measured"], bool))
    check("the data-plane gap is always stated",
          any("Activity Log" in l for l in u.get("limitations", [])))
    check("absence-of-use is explicitly not treated as proof",
          any("not proof of absence of need" in l for l in u.get("limitations", [])))
    for n in u.get("notes") or []:
        print(f"  note: {n}")

    # ---------------------------------------------------------------- right-sizing
    status, d = call(f"/iam/rightsizing{q}")
    if status != 200:
        print(f"GET /iam/rightsizing -> {status} {d}")
        return 1
    print(f"\n/iam/rightsizing  measured={d['measured']}  assessed={d['assessed']}  "
          f"recommendations={len(d['recommendations'])}")
    if d["measured"]:
        print(f"  action universe: {d.get('action_universe_size')} distinct actions")
        print(f"  break-glass excluded from recommendations: {d.get('break_glass_excluded', 0)}")

    check("data-plane roles are excluded while data-plane logging is unavailable",
          any("Data-plane roles were excluded" in e for e in d.get("excluded", [])))

    missing = [
        r["principalId"] for r in d["recommendations"]
        if not all(k in r for k in ("usedActionCount", "grantedActionCount", "window", "confidence"))
    ]
    check("every figure carries its denominator, window and confidence", not missing,
          ", ".join(missing[:3]))

    bad_bg = [
        r["principalName"] for r in d["recommendations"]
        if "Break-glass" in (r.get("note") or "") and r.get("recommendation")
    ]
    check("a break-glass account never carries a removal recommendation", not bad_bg,
          ", ".join(bad_bg[:3]))

    no_risk = [
        r["principalId"] for r in d["recommendations"]
        if r.get("recommendation") and not r["recommendation"].get("residualRisk")
    ]
    check("every proposal names what it gives up", not no_risk, ", ".join(no_risk[:3]))

    proposes_owner = [
        r["principalId"] for r in d["recommendations"]
        if r.get("recommendation") and any(
            role.lower() in ("owner", "user access administrator")
            for role in r["recommendation"]["roles"]
        )
    ]
    check("no proposal recommends Owner as the narrower option", not proposes_owner,
          ", ".join(proposes_owner[:3]))

    if d["recommendations"]:
        print("\n  top over-privileged")
        for r in d["recommendations"][:6]:
            prop = r.get("recommendation")
            print(f"    [{r['confidence']:<6}] {(r['principalName'] or r['principalId'])[:28]:<28} "
                  f"{r['currentRoles'][0][:20]:<20} used {r['usedActionCount']:>3} of "
                  f"{r['grantedActionCount']:<5} -> "
                  f"{', '.join(prop['roles']) if prop else '(no proposal)'}")

    # ---------------------------------------------------------------- simulator
    print("\nsimulator")
    status, kinds = call(f"/iam/simulate/kinds")
    check("the change-kind vocabulary is published", status == 200 and bool(kinds.get("kinds")))

    status, err = call(f"/iam/simulate{q}", {"changes": [{"kind": "delete_everything"}]})
    check("an unknown change kind is a 400, not an empty diff", status == 400, str(err)[:100])

    status, err = call(f"/iam/simulate{q}", {"changes": [{"kind": "remove_assignment"}]})
    check("a change missing a required field is a 400", status == 400, str(err)[:100])

    status, err = call(f"/iam/simulate{q}",
                       {"changes": [{"kind": "remove_assignment", "assignment_id": "not-a-real-id"}]})
    check("a change against a deleted referent is a 409, not a 400 or an empty diff",
          status == 409, str(err)[:120])

    # A real change, taken from the live snapshot.
    status, grid = call(f"/iam/access{q or '?'}{amp if q else ''}tab=privileged&limit=1".replace("??", "?"))
    rows = (grid or {}).get("rows") or [] if isinstance(grid, dict) else []
    if rows:
        aid = rows[0].get("assignmentId", "")
        status, sim = call(f"/iam/simulate{q}",
                           {"changes": [{"kind": "remove_assignment", "assignment_id": aid}]})
        if status != 200:
            check("a real assignment can be simulated", False, str(sim)[:160])
        else:
            print(f"  removing {rows[0].get('effectivePrincipalName')} / {rows[0].get('roleName')}:")
            print(f"    lost={len(sim['access_lost'])}  "
                  f"retained_via_other_path={len(sim['access_retained_via_other_path'])}  "
                  f"gained={len(sim['access_gained'])}  "
                  f"orphaned={len(sim['orphaned_resources'])}")
            print(f"    standing privilege {sim['standing_privilege_before']} -> {sim['standing_privilege_after']}")
            check("access_retained_via_other_path is present on every result",
                  "access_retained_via_other_path" in sim)
            check("the sample size and population are published at the top level",
                  {"sampled", "size", "population", "seed"} <= set(sim.get("sample", {})))
            check("limitations are always published", bool(sim.get("limitations")))
            check("the simulation states it is a model, not a prediction",
                  any("not a prediction" in l for l in sim.get("limitations", [])))
    else:
        print("  (no privileged rows in the snapshot to simulate against)")

    print("\n" + ("FAILURES: " + "; ".join(failures) if failures else "all checks passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
