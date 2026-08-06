"""Run the Azure Activity Log usage sweep once against a named connection.

Exists so the "last used" column and the `never used the access` filter can be verified against
real data rather than only synthetically. Read-only: it queries the Activity Log across the
connection's subscriptions and writes nothing to Azure.

    python scripts/iam_usage_refresh_live.py <connection-name-or-id> [days]

Selects the connection by display NAME (or id) at runtime, so no tenant identifier is written
down anywhere.
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
    with OPENER.open(req, timeout=1800) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    selector = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    call("/auth/login", {"username": "admin", "password": "admin"})

    conns = call("/azure/connections")["connections"]
    match = next(
        (c for c in conns if c["id"] == selector
         or str(c.get("display_name", "")).lower() == selector.lower()),
        None,
    )
    if not match:
        print(f"No connection matches {selector!r}.")
        return 1
    q = urllib.parse.urlencode({"connection_id": match["id"]})
    print(f"connection: {match.get('display_name')}   window: {days}d")

    started = time.monotonic()
    # connection_id is a QUERY parameter, not a body field. In the body it is silently ignored
    # and the sweep runs against the DEFAULT connection.
    out = call(f"/iam/usage/refresh?{q}", {"days": days})
    print(f"swept in {time.monotonic() - started:.0f}s")
    for key in ("status", "window_days", "subscriptions", "event_count", "measured"):
        if key in out:
            print(f"  {key:16} {out[key]}")
    principals = out.get("principals") or []
    print(f"  principals seen  {len(principals)}")
    with_stamp = [p for p in principals if p.get("lastSeen")]
    print(f"  with a last-seen {len(with_stamp)}")
    if with_stamp:
        newest = max(p["lastSeen"] for p in with_stamp)
        oldest = min(p["lastSeen"] for p in with_stamp)
        print(f"  range            {oldest[:10]} .. {newest[:10]}")

    rep = call(f"/iam/leavers?{q}")
    print("\nleavers report after the sweep:")
    print("  usage:", rep.get("usage"))
    measured = [i for i in rep.get("identities", []) if i.get("activityMeasured")]
    used = [i for i in measured if i.get("lastActivity")]
    print(f"  identities with usage measured: {len(measured)}")
    print(f"  ...of which ever used the access: {len(used)}")
    print(f"  never used (a real, measured zero): {len(measured) - len(used)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
