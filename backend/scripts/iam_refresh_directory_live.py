"""Run a live IAM **directory** refresh against a named connection and report what it collected.

Exists so the account-state collector can be exercised against real Microsoft Graph without
running a full estate rescan. Selects the connection by display NAME (or id) at runtime, so no
tenant identifier is written down anywhere.

    python scripts/iam_refresh_directory_live.py <connection-name-or-id>
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
        # main.py rejects cookie-bearing state-changing requests without proof of same-origin.
        req.add_header("Sec-Fetch-Site", "same-origin")
        req.add_header("Origin", "http://127.0.0.1:8000")
    with OPENER.open(req, timeout=600) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    selector = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    call("/auth/login", {"username": "admin", "password": "admin"})

    conns = call("/azure/connections")["connections"]
    match = next(
        (c for c in conns if c["id"] == selector
         or str(c.get("display_name", "")).lower() == selector.lower()),
        None,
    )
    if not match:
        print(f"No connection matches {selector!r}. Available: "
              + ", ".join(str(c.get("display_name")) for c in conns))
        return 1
    cid = match["id"]
    q = urllib.parse.urlencode({"connection_id": cid})
    print(f"connection: {match.get('display_name')}")

    print("starting directory refresh…")
    # connection_id is a QUERY parameter on /iam/refresh, not a body field. Passing it in the
    # body is silently ignored and the refresh runs against the DEFAULT connection — a
    # wrong-tenant collection that reports success and leaves the intended tenant untouched.
    started = call(f"/iam/refresh?{q}", {"mode": "directory"})
    if started.get("already_running"):
        print("  (a directory refresh was already in flight; following it)")

    # Poll the job rather than /iam/runs: runs[0] is the PREVIOUS run and reads "succeeded"
    # instantly, which looks like a refresh that finished before it started.
    last = ""
    seen_job = False
    for _ in range(600):
        job = call(f"/iam/job?mode=directory&{q}").get("job") or {}
        if not job:
            # Never seeing a job at all means the POST targeted a different tenant. Say so
            # rather than spinning for twenty minutes against an empty key.
            if seen_job:
                break
            time.sleep(2)
            continue
        seen_job = True
        msg = str(job.get("last_message") or "")
        if msg and msg != last:
            print("  ", msg[:150])
            last = msg
        if job.get("status") in ("done", "succeeded", "failed", "error"):
            print("job:", job.get("status"), job.get("error") or "")
            break
        time.sleep(2)
    if not seen_job:
        print("No job appeared for this tenant — the refresh did not target the connection asked for.")
        return 1

    ov = call(f"/iam/overview?{q}")["kpis"]
    print("\nKPIs")
    for k in ("total_assignments", "unique_principals", "account_state_collected",
              "disabled_principals", "disabled_assignments", "disabled_privileged"):
        print(f"  {k:26} {ov.get(k)}")

    rep = call(f"/iam/leavers?{q}")
    print("\nreport")
    print("  measured:", rep["measured"])
    print("  denominator:", rep["denominator"])
    print("  tier_counts:", rep["tier_counts"])
    print("  totals:", rep["totals"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
