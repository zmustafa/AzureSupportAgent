"""Why did the effective-permission engine answer `indeterminate` for these principals?

A high indeterminate rate is not automatically a bug, but it IS automatically worth explaining:
every one of them is a question the product declined to answer.
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
    with OPENER.open(req, timeout=120) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    q = f"&connection_id={conn}" if conn else ""
    get("/auth/login", {"username": "admin", "password": "admin"})

    page = get(f"/iam/access?tab=privileged&limit=50{q}")
    row = next(r for r in page["rows"] if r.get("effect") != "Deny")
    scope = row["scope"]
    action = "Microsoft.Compute/virtualMachines/delete"

    wc = get(f"/iam/resource-access?scope={scope}&action={action}{q}")
    print(f"scope: {scope}")
    print(f"{len(wc['allowed'])} allowed / {len(wc['indeterminate'])} indeterminate "
          f"of {wc['candidates']} candidates\n")

    for p in wc["indeterminate"]:
        d = get(f"/iam/effective?principal_id={p['principalId']}&scope={scope}&action={action}{q}")
        print(f"  {p['principalName'] or p['principalId']}")
        print(f"    unknownRoles: {d['unknownRoles']}")
        print(f"    conditions:   {len(d['conditionUnevaluated'])}")
        print(f"    reason:       {d['reason'][:150]}")

    # What surfaces do those unresolved role names come from?
    rows = get(f"/iam/access?tab=all&limit=500{q}")["rows"]
    unresolved = {n for p in wc["indeterminate"]
                  for n in get(f"/iam/effective?principal_id={p['principalId']}&scope={scope}&action={action}{q}")["unknownRoles"]}
    print("\nsurface of each unresolved role name:")
    for name in sorted(unresolved):
        surfaces = {r.get("surface") for r in rows if r.get("roleName") == name}
        print(f"  {name[:60]:60} {surfaces or '(not in the first 500 rows)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
