"""Print the live IAM overview, score and per-scope source/freshness after a refresh.

A quick end-to-end read of what the ARG pivot actually produced, against the running local API.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.request

JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def get(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:8000/api" + path, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=90) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    q = f"?connection_id={conn}" if conn else ""
    get("/auth/login", {"username": "admin", "password": "admin"})

    kpis = get("/iam/overview" + q)["kpis"]
    print("KPIs:")
    for k, v in kpis.items():
        print(f"  {k:24} {v}")

    s = get("/iam/score" + q)
    print(f"\nscore={s['score']} coverage={s['coverage']} grade={s['grade']}")

    print("\nscopes:")
    for sc in get("/iam/scopes" + q)["scopes"]:
        print(
            f"  {sc['displayName'][:26]:26} {sc['status']:20} src={sc.get('source', '') or '-':4} "
            f"rows={sc['row_count']:4} verified={str(sc.get('verified_unchanged')):5} stale={sc.get('stale')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
