"""Live smoke for the shadow-access (bypass) endpoint.

The unit tests cover the rules. This checks they are wired to real cached data and that the
three claims this screen cannot get wrong hold on a real tenant:

  1. the RBAC-only percentage is never published without its denominator, and is null (not 0)
     when nothing was assessed;
  2. a service family that could not be read reports its status — it never contributes a clean
     zero to the numbers;
  3. every finding carries the `breaksIf` warning that qualifies its remediation.

    .venv\\Scripts\\python.exe scripts\\iam_bypass_smoke.py [connection_id]
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

# Only these mean "we could not read this family". The first version of this script guessed a
# status called "Collected" that the collector never emits, and duly flagged every healthy
# family as unreadable — the same mistake the UI made.
UNREADABLE = {"Unauthorized", "Throttled", "Failed"}


def call(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode(errors="replace")


def main() -> int:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    q = f"?connection_id={conn}" if conn else ""
    call("/auth/login", {"username": "admin", "password": "admin"})

    failures: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")
        if not cond:
            failures.append(label)

    status, d = call(f"/iam/bypass{q}")
    if status != 200 or not isinstance(d, dict):
        print(f"GET /iam/bypass -> {status} {d}")
        return 1

    s = d["summary"]
    rows = d["rows"]
    print(f"\n/iam/bypass  status={d['status']}  rows={len(rows)}  never_loaded={d['never_loaded']}")
    print(f"  assessed={s['assessed']}  rbac_only={s['rbac_only']}  bypassed={s['bypassed']}  "
          f"pct={s['rbac_only_pct']}")

    print("\ncontract")
    check("denominator is published alongside the ratio", "assessed" in s)
    check(
        "ratio is null rather than 0 when nothing was assessed",
        s["rbac_only_pct"] is not None if s["assessed"] else s["rbac_only_pct"] is None,
        f"assessed={s['assessed']} pct={s['rbac_only_pct']}",
    )
    check("rbac_only + bypassed == assessed", s["rbac_only"] + s["bypassed"] == s["assessed"])
    check(
        "the scope limitation is always published",
        any("door, not the room" in l for l in s["limitations"]),
    )

    missing_breaks = [r["key"] for r in rows if not r.get("breaksIf")]
    check("every finding carries breaksIf", not missing_breaks, ", ".join(missing_breaks[:3]))
    missing_rem = [r["key"] for r in rows if not r.get("remediation")]
    check("every finding carries remediation", not missing_rem, ", ".join(missing_rem[:3]))

    print("\nper family")
    blind = []
    for f in sorted(s["by_family"], key=lambda x: x["family"]):
        flag = f"  <-- {f['status']}" if f["status"] in UNREADABLE else ""
        if f["status"] in UNREADABLE:
            blind.append(f["family"])
        print(f"  {f['family']:<12} assessed={f['assessed']:<5} affected={f['affected']:<4} "
              f"findings={f['findings']:<4} {f['status']}{flag}")
    check(
        "an unreadable family is named in the limitations, not silently zero",
        not blind or any(f in " ".join(s["limitations"]) for f in blind),
        ", ".join(blind),
    )

    # Blind != nobody: a row whose reachability join could not run must say so explicitly.
    unavail = [r for r in rows if r.get("credentialAction") and not r["reachabilityAvailable"]]
    print(f"\nreachability unavailable on {len(unavail)} of "
          f"{sum(1 for r in rows if r.get('credentialAction'))} credential-bearing rows")

    if rows:
        print("\ntop findings")
        for r in rows[:8]:
            who = (
                f"{r['reachableCount']} can fetch" if r["reachabilityAvailable"]
                else "reach unknown"
            )
            print(f"  [{r['severity']:<8}] {r['resourceName']:<28} {r['title']:<38} "
                  f"env={r['environment'] or '-':<8} {who}")

    print("\n" + ("FAILURES: " + "; ".join(failures) if failures else "all checks passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
