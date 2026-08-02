"""Live smoke for the escalation graph + managed-identity endpoints.

The unit tests cover the rules; this checks they are wired to real cached data and that the
graph contract Cytoscape depends on holds on a real tenant.

    .venv\\Scripts\\python.exe scripts\\iam_escalation_smoke.py [connection_id]
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
    amp = "&" if q else "?"
    call("/auth/login", {"username": "admin", "password": "admin"})

    failures: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")
        if not cond:
            failures.append(label)

    print("GET /iam/escalation")
    st, g = call(f"/iam/escalation{q}")
    check("200", st == 200, str(g)[:150])
    if st != 200:
        return 1
    print(f"       {g['stats']['node_count']} nodes, {g['stats']['edge_count']} edges, "
          f"{len(g['paths'])} path(s), {g['dropped_edges']} dropped")

    # THE contract. One edge pointing at a missing node makes Cytoscape reject the whole batch.
    present = {n["id"] for n in g["nodes"]}
    dangling = [e for e in g["edges"] if e["source"] not in present or e["target"] not in present]
    check("no edge points at a missing node", not dangling, f"{len(dangling)} dangling")
    check("no self-loops", not [e for e in g["edges"] if e["source"] == e["target"]])
    check("limitations are published", bool(g["limitations"]))
    for lim in g["limitations"]:
        print(f"       ! {lim[:110]}")

    print("paths")
    for p in g["paths"][:5]:
        print(f"       {p['length']} hop(s) {p['fromLabel'][:32]:32} via "
              f"{'->'.join(h['primitive'] for h in p['hops'])} [{p['min_confidence']}]")
    check("every path ends at full control",
          all(p["to"] == "tier0::owner" for p in g["paths"]))
    check("paths are sorted shortest-first",
          [p["length"] for p in g["paths"]] == sorted(p["length"] for p in g["paths"]))

    print("confidence filter")
    st, high = call(f"/iam/escalation{q}{amp}min_confidence=high")
    check("high-only is a subset of all", high["stats"]["edge_count"] <= g["stats"]["edge_count"],
          f"{high['stats']['edge_count']} vs {g['stats']['edge_count']}")
    st, bad = call(f"/iam/escalation{q}{amp}min_confidence=nonsense")
    check("an invalid confidence is rejected", st == 400, f"status={st}")

    print("GET /iam/identities")
    st, ids = call(f"/iam/identities{q}")
    check("200", st == 200, str(ids)[:150])
    if st == 200:
        print(f"       {ids['total']} managed identity/identities, "
              f"{ids['federated_total']} federated credential(s)")
        priv = [i for i in ids["identities"] if i["privileged"]]
        print(f"       {len(priv)} privileged")
        for i in ids["identities"][:5]:
            attached = i.get("attachedResourceIds") or []
            print(f"       {i['identityKind']:15} {i['identityName'][:28]:28} "
                  f"roles={i['roles'][:2]} attached={len(attached)}")
        check("privileged identities sort first",
              [i["privileged"] for i in ids["identities"]] == sorted(
                  (i["privileged"] for i in ids["identities"]), reverse=True))

    print("legacy /rbac alias")
    st, _ = call(f"/rbac/escalation{q}")
    check("alias still serves the graph", st == 200, f"status={st}")

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
