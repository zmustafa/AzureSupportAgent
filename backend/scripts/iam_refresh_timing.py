"""Measure a real IAM refresh: ARM per-scope versus the Resource Graph sweep, then delta.

The P3 acceptance criterion is a wall-clock one, so it has to be measured against a real tenant
rather than asserted in a unit test. Runs against the local API on :8000.

    .venv\\Scripts\\python.exe scripts\\iam_refresh_timing.py [connection_id]
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    if method not in ("GET", "HEAD"):
        req.add_header("Sec-Fetch-Site", "same-origin")
    try:
        with OPENER.open(req, timeout=900) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400].decode(errors="replace")


def run_refresh(mode: str, connection_id: str | None) -> tuple[float, list[str]]:
    """Start a refresh and block until the job leaves 'running'. Returns (seconds, log)."""
    qs = f"?connection_id={connection_id}" if connection_id else ""
    started = time.monotonic()
    st, _ = call("POST", f"/iam/refresh{qs}", {"mode": mode})
    if st != 200:
        raise SystemExit(f"refresh start failed: {st}")
    job_mode = "all" if mode in ("all", "delta") else mode
    sep = "&" if qs else "?"
    while True:
        time.sleep(2)
        st, out = call("GET", f"/iam/job{qs}{sep}mode={job_mode}")
        job = (out or {}).get("job") or {}
        if job.get("status") != "running":
            elapsed = time.monotonic() - started
            return elapsed, [p.get("message", "") for p in job.get("progress", [])]


def scope_summary(connection_id: str | None) -> dict:
    qs = f"?connection_id={connection_id}" if connection_id else ""
    _st, out = call("GET", f"/iam/scopes{qs}")
    scopes = out.get("scopes", [])
    return {
        "count": len(scopes),
        "rows": sum(s.get("row_count", 0) for s in scopes),
        "arg": sum(1 for s in scopes if s.get("source") == "arg"),
        "arm": sum(1 for s in scopes if s.get("source") == "arm"),
        "verified": sum(1 for s in scopes if s.get("verified_unchanged")),
        "attention": sum(1 for s in scopes if s.get("collectors_attention")),
    }


def main() -> int:
    connection_id = sys.argv[1] if len(sys.argv) > 1 else None
    st, out = call("POST", "/auth/login", {"username": "admin", "password": "admin"})
    if st != 200:
        print(f"login failed: {st} {out}")
        return 1

    print(f"connection: {connection_id or '(default)'}")
    print("\n--- full refresh -------------------------------------------------")
    secs, log = run_refresh("all", connection_id)
    before = scope_summary(connection_id)
    print(f"  {secs:.1f}s  scopes={before['count']} rows={before['rows']} "
          f"arg={before['arg']} arm={before['arm']} attention={before['attention']}")
    for line in log:
        if any(k in line.lower() for k in ("resource graph", "warning", "failed", "verifying", "missing")):
            print(f"     {line}")

    print("\n--- delta refresh (immediately after) ----------------------------")
    secs2, log2 = run_refresh("delta", connection_id)
    after = scope_summary(connection_id)
    print(f"  {secs2:.1f}s  verified-unchanged={after['verified']} of {after['count']} scopes")
    for line in log2:
        if any(k in line.lower() for k in ("delta", "changed", "resource graph", "warning")):
            print(f"     {line}")

    if after["rows"] != before["rows"]:
        print(f"  NOTE rows changed across the delta pass: {before['rows']} -> {after['rows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
