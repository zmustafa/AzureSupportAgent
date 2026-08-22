"""Time the IAM read endpoints on a tenant, to catch a screen that is quietly too slow.

The escalation graph runs the effective-permission engine across every principal, primitive and
scope, so it is the one endpoint whose cost can grow superlinearly with the estate.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.request

JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def get(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:35001/api" + path, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    started = time.monotonic()
    with OPENER.open(req, timeout=600) as resp:
        payload = json.loads(resp.read() or b"{}")
    return time.monotonic() - started, payload


def main() -> int:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    q = f"?connection_id={conn}" if conn else ""
    get("/auth/login", {"username": "admin", "password": "admin"})

    dt, ov = get("/iam/overview" + q)
    print(f"  overview      {dt:7.2f}s  rows={ov.get('kpis', {}).get('total_assignments')} "
          f"principals={ov.get('kpis', {}).get('unique_principals')} scopes={ov.get('kpis', {}).get('scopes')}")

    for label in ("escalation", "escalation (warm)"):
        dt, g = get("/iam/escalation" + q)
        print(f"  {label:14}{dt:7.2f}s  nodes={g['stats']['node_count']} "
              f"edges={g['stats']['edge_count']} paths={len(g['paths'])}")

    dt, f = get("/iam/findings" + q)
    print(f"  findings      {dt:7.2f}s  total={f.get('total')}")
    dt, _s = get("/iam/score" + q)
    print(f"  score         {dt:7.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
