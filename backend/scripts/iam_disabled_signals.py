"""Ad-hoc: print the evaluation state of the disabled-access signals for one connection.

Kept because "the signal is registered" and "the signal actually ran on this tenant" are
different facts, and the findings endpoint is PAGED — a signal missing from a page of 200 on a
5,500-row tenant proves nothing at all. This reads the registry evaluation directly.

    python scripts/iam_disabled_signals.py <connection-name-or-id>
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
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
        req.add_header("Sec-Fetch-Site", "same-origin")
        req.add_header("Origin", "http://127.0.0.1:8000")
    with OPENER.open(req, timeout=300) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    selector = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    call("/auth/login", {"username": "admin", "password": "admin"})
    q = ""
    if selector:
        conns = call("/azure/connections")["connections"]
        match = next(
            (c for c in conns if c["id"] == selector
             or str(c.get("display_name", "")).lower() == selector.lower()),
            None,
        )
        if not match:
            print(f"No connection matches {selector!r}.")
            return 1
        print(f"connection: {match.get('display_name')}")
        q = "?" + urllib.parse.urlencode({"connection_id": match["id"]})

    payload = call(f"/iam/signals{q}")
    print("\nkeys:", sorted(payload.keys()))
    rows = payload.get("signals") or payload.get("results") or []
    interesting = [
        s for s in rows
        if "disabled" in str(s.get("id") or s.get("signal_id"))
        or "deleted_principal" in str(s.get("id") or s.get("signal_id"))
    ]
    if not interesting:
        print("no disabled signals in the response; sample entry:")
        print(json.dumps(rows[0] if rows else {}, indent=2)[:800])
        return 1
    for s in interesting:
        print(
            f"  {str(s.get('id') or s.get('signal_id')):38} "
            f"measured={s.get('measured')} findings={s.get('finding_count', s.get('count'))} "
            f"{str(s.get('reason') or '')[:60]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
