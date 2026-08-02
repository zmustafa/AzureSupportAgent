"""Live smoke for the IAM findings API against a running local server.

The unit tests exercise the service layer; this exercises the wire: auth, routing, the /rbac
alias, query filters, and the write path. Run with the API on :8000.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"

# The app authenticates with a session cookie, not a bearer token.
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))
ANON = urllib.request.build_opener()  # deliberately shares no cookies


def call(method: str, path: str, body: dict | None = None, *, anon: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    # State-changing cookie-bearing requests are rejected without proof of same-origin -- the
    # CSRF guard in main.py. A browser sets this automatically; a script has to say so.
    if method not in ("GET", "HEAD", "OPTIONS"):
        req.add_header("Sec-Fetch-Site", "same-origin")
    opener = ANON if anon else OPENER
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read()[:300]).decode(errors="replace")


def main() -> int:
    status, out = call("POST", "/auth/login", {"username": "admin", "password": "admin"})
    if status != 200:
        print(f"login failed: {status} {out}")
        return 1

    failures: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")
        if not cond:
            failures.append(label)

    print("GET /iam/findings")
    st, f = call("GET", "/iam/findings")
    check("200", st == 200, str(f)[:150])
    if st != 200:
        return 1
    check("has counts_by_severity", "counts_by_severity" in f)
    check("publishes unmeasured signals", isinstance(f.get("unmeasured"), list))
    print(f"       total={f['total']} unmeasured={len(f['unmeasured'])} severities={f['counts_by_severity']}")

    print("GET /iam/score")
    st, s = call("GET", "/iam/score")
    check("200", st == 200, str(s)[:150])
    check("coverage always accompanies the score", "coverage" in s and "score" in s)
    check(
        "grade withheld below the coverage floor",
        s["grade"] is None or s["coverage"] >= s["min_coverage_for_grade"],
        f"grade={s['grade']} coverage={s['coverage']}",
    )
    blind = [p for p in s["pillars"] if p["score"] is None]
    check("unmeasured pillars report null, not 0/100",
          all(p["state"] in ("blind", "not_implemented") for p in blind))
    print(f"       score={s['score']} coverage={s['coverage']} grade={s['grade']}")
    for p in s["pillars"]:
        print(f"       {p['key']:5} {str(p['score']):>5} {p['state']}")

    print("GET /iam/signals")
    st, cat = call("GET", "/iam/signals")
    check("200", st == 200)
    check("pillar weights total 100", sum(p["weight"] for p in cat["pillars"]) == 100)
    print(f"       {len(cat['signals'])} signals across {len(cat['pillars'])} pillars")

    print("filters")
    st, err = call("GET", "/iam/findings?severity=error")
    check("severity filter applies", st == 200 and all(i["severity"] == "error" for i in err["findings"]))
    st, bad = call("GET", "/iam/findings?limit=99999")
    check("limit is bounded", st == 422 or (st == 200 and bad["limit"] <= 500), f"status={st}")

    print("legacy /rbac alias")
    st, _ = call("GET", "/rbac/findings")
    check("alias still serves findings", st == 200, f"status={st}")

    if f["findings"]:
        fp = f["findings"][0]["id"]
        print(f"state round-trip on {fp}")
        st, _ = call("POST", f"/iam/findings/{fp}/state", {"state": "suppressed", "reason": "smoke"})
        check("suppress accepted", st == 200, f"status={st}")
        st, after = call("GET", "/iam/findings")
        check("suppressed finding hidden by default", all(i["id"] != fp for i in after["findings"]))
        st, incl = call("GET", "/iam/findings?include_suppressed=true")
        check("suppressed finding retrievable", any(i["id"] == fp for i in incl["findings"]))
        st, _ = call("POST", f"/iam/findings/{fp}/state", {"state": "open", "reason": ""})
        check("restored to open", st == 200)
        st, _ = call("POST", f"/iam/findings/{fp}/state", {"state": "nonsense", "reason": ""})
        check("invalid state rejected", st == 400, f"status={st}")

    print("unauthenticated access")
    st, _ = call("GET", "/iam/findings", anon=True)
    check("requires auth", st in (401, 403), f"status={st}")

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
