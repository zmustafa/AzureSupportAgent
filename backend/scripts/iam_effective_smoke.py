"""Live smoke for the effective-permission endpoints against the running local API.

Exercises the wire (auth, routing, the /rbac alias) and, more importantly, checks the answers
against a REAL tenant's cached access rather than a synthetic fixture — the unit tests already
cover the rules, this checks they are wired to real data.

    .venv\\Scripts\\python.exe scripts\\iam_effective_smoke.py [connection_id]
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

VM_DELETE = "Microsoft.Compute/virtualMachines/delete"
BLOB_READ = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"
ROLE_WRITE = "Microsoft.Authorization/roleAssignments/write"


def call(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode(errors="replace")


def main() -> int:
    conn = sys.argv[1] if len(sys.argv) > 1 else ""
    q = f"&connection_id={conn}" if conn else ""
    call("/auth/login", {"username": "admin", "password": "admin"})

    failures: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")
        if not cond:
            failures.append(label)

    # Find a real privileged principal to ask about.
    st, page = call(f"/iam/access?tab=privileged&limit=50{q}")
    if st != 200 or not page.get("rows"):
        print(f"no privileged rows to test against (status {st})")
        return 1
    row = next(
        (r for r in page["rows"]
         if r.get("roleName") == "Owner" and r.get("effect") != "Deny"
         and r.get("assignmentState") != "Eligible"),
        page["rows"][0],
    )
    pid = row.get("effectivePrincipalId") or row.get("principalId")
    scope = row.get("scope")
    name = row.get("effectivePrincipalName") or pid
    print(f"subject: {name} | {row.get('roleName')} @ {scope}\n")

    print("GET /iam/effective (control plane)")
    st, d = call(f"/iam/effective?principal_id={pid}&scope={scope}&action={VM_DELETE}{q}")
    check("200", st == 200, str(d)[:120])
    if st != 200:
        return 1
    print(f"       verdict={d['verdict']}  {d['reason'][:110]}")
    check("an Owner is allowed to delete a VM", d["verdict"] == "allowed", d["verdict"])
    check("the deciding assignment is named", bool(d.get("decidedBy")))
    check("the plane was inferred as control", d["plane"] == "control")

    print("GET /iam/effective (data plane)")
    st, d2 = call(f"/iam/effective?principal_id={pid}&scope={scope}&action={BLOB_READ}{q}")
    print(f"       verdict={d2['verdict']}  {d2['reason'][:110]}")
    check("the plane was inferred as data", d2["plane"] == "data")
    # Owner has actions ["*"] and no dataActions, so it must NOT grant blob data access.
    check(
        "a control-plane Owner does not grant blob data read",
        d2["verdict"] in ("not_granted", "denied", "indeterminate"),
        d2["verdict"],
    )

    print("verdict vocabulary")
    st, d3 = call(f"/iam/effective?principal_id={pid}&scope={scope}&action=Microsoft.Nope/x/y{q}")
    check("the verdict is always one of the four",
          d3["verdict"] in ("allowed", "denied", "not_granted", "indeterminate"), d3["verdict"])
    # NOTE: a nonexistent action CAN legitimately be "allowed" — Owner and Contributor carry
    # actions: ["*"], and Azure's wildcard really does match any action string. Asserting
    # otherwise tests a rule Azure does not have.

    print("the decider is stable")
    st, again = call(f"/iam/effective?principal_id={pid}&scope={scope}&action={VM_DELETE}{q}")
    check(
        "the same question returns the same deciding assignment",
        (again.get("decidedBy") or {}).get("assignmentId") == (d.get("decidedBy") or {}).get("assignmentId"),
    )
    check("every granting assignment is listed, not just the decider",
          len(d["grantingAssignments"]) >= 1, str(len(d["grantingAssignments"])))

    print("grant set (Principal 360)")
    st, gs = call(f"/iam/principal/{pid}/access?scope={scope}{q}")
    check("200", st == 200, str(gs)[:120])
    if st == 200:
        check("returns roles, not expanded action strings", "control" in gs and "data" in gs)
        print(f"       control={len(gs.get('control', []))} data={len(gs.get('data', []))} "
              f"denies={len(gs.get('denies', []))} unknown={gs.get('unknownRoles')}")

    print("inverse pivot (Resource 360)")
    st, wc = call(f"/iam/resource-access?scope={scope}&action={VM_DELETE}{q}")
    check("200", st == 200, str(wc)[:120])
    if st == 200:
        print(f"       {len(wc['allowed'])} allowed, {len(wc['indeterminate'])} indeterminate "
              f"of {wc['candidates']} candidate(s)")
        check("the subject appears in the allowed list",
              any(p["principalId"].lower() == str(pid).lower() for p in wc["allowed"]))
        check("indeterminate is kept separate from allowed", "indeterminate" in wc)

    print("legacy /rbac alias")
    st, _ = call(f"/rbac/effective?principal_id={pid}&scope={scope}&action={VM_DELETE}{q}")
    check("alias still serves the engine", st == 200, f"status={st}")

    print("validation")
    st, _ = call(f"/iam/effective?principal_id={pid}&scope={scope}&action={VM_DELETE}&plane=nonsense{q}")
    check("an invalid plane is rejected", st == 400, f"status={st}")

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
