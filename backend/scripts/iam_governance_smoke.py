"""Live smoke for the P7 governance workflow.

Drives a real campaign end to end against the running API — create, activate, decide, generate
remediation, complete, export evidence — and checks the four claims this feature cannot get
wrong:

  1. an expired or completed campaign with undecided items reports itself INCOMPLETE and never
     auto-approves;
  2. every generated remediation action carries a rollback and a `breaksIf`, and no artifact
     contains a credential;
  3. an unattributed change is `unknown`, never blank;
  4. a framework control that nothing measured is `not_measured`, never `pass`.

    .venv\\Scripts\\python.exe scripts\\iam_governance_smoke.py [connection_id]
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:35001/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

SECRETS = (
    re.compile(r"connectionstring\s*=", re.I),
    re.compile(r"accountkey\s*=", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\."),
)


def call(path: str, body: dict | None = None, method: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("Sec-Fetch-Site", "same-origin")
    req.add_header("Origin", "http://127.0.0.1:35001")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400].decode(errors="replace")


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

    # ---------------------------------------------------------------- diff
    status, d = call(f"/iam/diff{q}")
    if status != 200 or not isinstance(d, dict):
        print(f"GET /iam/diff -> {status} {d}")
        return 1
    print(f"\n/iam/diff  available={d.get('available')}  total={d.get('total')}")
    counts = {k: v for k, v in (d.get("counts_by_class") or {}).items() if v}
    print(f"  by class: {counts or '(none)'}")
    check(
        "an unavailable diff says so rather than showing an empty change list",
        "available" in d,
    )
    if not d.get("available"):
        check("an unavailable diff carries no changes", not d.get("changes"))
    for c in (d.get("changes") or [])[:200]:
        if c.get("actor") is None:
            check("every change carries an actor block, even when unknown", False, c.get("key", ""))
            break
    else:
        check("every change carries an actor block, even when unknown", True)

    # ---------------------------------------------------------------- frameworks
    status, fw = call(f"/iam/frameworks{q}")
    if status != 200:
        print(f"GET /iam/frameworks -> {status} {fw}")
        return 1
    print("\n/iam/frameworks")
    for f in fw.get("by_framework", []):
        print(f"  {f['framework']:<12} {f['controls']:>3} controls  "
              f"{f['passing']} pass / {f['failing']} fail / {f['not_measured']} not measured")
    blind = [c for c in fw.get("controls", []) if c["state"] == "not_measured"]
    wrong = [c for c in fw.get("controls", []) if c["state"] == "pass" and c["measured_signals"] == 0]
    check("a control nothing measured is never reported as passing", not wrong,
          ", ".join(f"{c['framework']}:{c['control']}" for c in wrong[:3]))
    check("the mapping states it is not a full framework assessment",
          any("NOT a full assessment" in l for l in fw.get("limitations", [])))
    print(f"  {len(blind)} control(s) reported not_measured")

    # ---------------------------------------------------------------- campaign lifecycle
    print("\ncampaign lifecycle")
    status, created = call(f"/iam/campaigns{q}", {
        "name": "smoke: privileged certification",
        "selector": {"kind": "privileged"},
        "reviewer_strategy": "owner",
    })
    if status != 200:
        print(f"  POST /iam/campaigns -> {status} {created}")
        return 1
    cid = created["campaign"]["id"]
    total = created["campaign"]["stats"].get("total", 0)
    print(f"  created {cid} with {total} item(s)")

    status, _ = call(f"/iam/campaigns/{cid}/activate{q}", {})
    check("a draft campaign activates", status == 200)

    status, detail = call(f"/iam/campaigns/{cid}{q}")
    items = detail.get("items", [])
    check("items are listed with their reviewer context", bool(items))
    self_reviewed = [i for i in items if i["reviewer_id"] and i["reviewer_id"].lower() == i["principalId"].lower()]
    check("nobody is assigned to review their own access", not self_reviewed,
          ", ".join(i["principalName"] for i in self_reviewed[:3]))
    no_usage_claim = [i for i in items if i["context"].get("usage") not in (None,)]
    check("no item presents unmeasured usage as a fact", not no_usage_claim)

    # Decide a couple so remediation has something to generate.
    decided = 0
    for item in items[:2]:
        status, _r = call(f"/iam/campaigns/{cid}/items/{item['id']}/decide{q}",
                          {"decision": "revoke", "reason": "smoke test"})
        decided += status == 200
    print(f"  recorded {decided} revoke decision(s) of {len(items)} item(s)")

    # ---------------------------------------------------------------- remediation
    print("\nremediation artifacts")
    for fmt in ("az", "powershell", "bicep", "terraform"):
        status, r = call(f"/iam/campaigns/{cid}/remediation{q}", {"format": fmt})
        bundle = (r or {}).get("bundle") if isinstance(r, dict) else None
        if status != 200 or not bundle:
            check(f"{fmt}: a bundle is generated", False, str(r)[:120])
            continue
        script = bundle["script"]
        has_rollback = "ROLLBACK" in script and all(a["rollback"] in script for a in bundle["actions"])
        has_breaks = all(a["breaks_if"] for a in bundle["actions"])
        leaked = [p.pattern for p in SECRETS if p.search(script)]
        check(f"{fmt}: every action has a rollback in the script", has_rollback)
        check(f"{fmt}: every action states what it breaks", has_breaks)
        check(f"{fmt}: no credential appears in the artifact", not leaked, ", ".join(leaked))
        check(f"{fmt}: the script says the product does not run it", "NOT RUN BY THE PRODUCT" in script)

    # ---------------------------------------------------------------- completion + evidence
    print("\ncompletion")
    status, completed = call(f"/iam/campaigns/{cid}/complete{q}", {})
    if status != 200:
        print(f"  complete -> {status} {completed}")
        return 1
    stats = completed["campaign"]["stats"]
    print(f"  decided {stats.get('decided')}/{stats.get('total')}  complete={stats.get('complete')}")
    check(
        "a campaign closed with undecided items reports itself incomplete",
        stats.get("complete") is (stats.get("decided") == stats.get("total")),
        f"decided={stats.get('decided')} total={stats.get('total')} complete={stats.get('complete')}",
    )
    check("undecided items are counted, not approved",
          stats.get("undecided", 0) == stats.get("total", 0) - stats.get("decided", 0))
    approved = (stats.get("by_decision") or {}).get("approve", 0)
    check("nothing was auto-approved on completion", approved == 0, f"approve={approved}")

    status, ev = call(f"/iam/campaigns/{cid}/evidence{q}", {})
    if status != 200:
        check("evidence export succeeds", False, str(ev)[:200])
    else:
        sha = (ev.get("evidence") or {}).get("sha256") or ev.get("digest", "")
        print(f"\nevidence  sha256={sha[:32]}…")
        check("the evidence pack is hashed", bool(sha))

    print("\n" + ("FAILURES: " + "; ".join(failures) if failures else "all checks passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
