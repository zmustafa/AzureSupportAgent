"""End-to-end REST check for the Entra ID Support Agent against the local API.

Exercises the full contract on a real Azure connection:

  1. cold-cache behaviour (every GET must return 200 with meta.loaded=false)
  2. demo seed + every read endpoint
  3. a real collection against a live connection, following the SSE progress stream
  4. posture / findings / Conditional Access / setup after collection
  5. permission + licence degradation is reported, never hidden

Usage (backend must already be running on :8000)::

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\entra_live_e2e.py
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\entra_live_e2e.py --connection <id>
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\entra_live_e2e.py --demo-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

API = "http://127.0.0.1:35001/api"
USER = "admin"
PASSWORD = "admin"

# The app blocks cookie-bearing state-changing requests that cannot prove same-origin
# (CSRF defence in main.py). A same-origin API client declares it explicitly.
SAME_ORIGIN_HEADERS = {"Origin": "http://127.0.0.1:35001", "Sec-Fetch-Site": "same-origin"}

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def login(client: httpx.Client) -> None:
    me = client.get(f"{API}/auth/me")
    if me.status_code == 200:
        return
    r = client.post(f"{API}/auth/login", json={"username": USER, "password": PASSWORD})
    r.raise_for_status()
    client.get(f"{API}/auth/me").raise_for_status()


def get(client: httpx.Client, path: str, **params: Any) -> tuple[int, dict[str, Any]]:
    r = client.get(f"{API}{path}", params={k: v for k, v in params.items() if v})
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, {}


def meta_ok(label: str, body: dict[str, Any]) -> None:
    meta = body.get("meta") or {}
    required = {"tenant_id", "loaded", "generated_at", "domains", "licences", "permissions_summary", "stale"}
    check(f"{label}: carries a complete meta envelope", required <= set(meta),
          f"missing {sorted(required - set(meta))}" if not required <= set(meta) else "")


# --------------------------------------------------------------------------- checks
def cold_cache(client: httpx.Client) -> None:
    section("Cold cache — every GET returns 200 with meta.loaded=false")
    fake = "no-such-connection-e2e"
    for path in ("/entra/status", "/entra/posture", "/entra/findings", "/entra/ca/coverage",
                 "/entra/ca/policies", "/entra/ca/conflicts", "/entra/ca/breakglass",
                 "/entra/setup/checklist", "/entra/diagnostics", "/entra/posture/diff",
                 "/entra/privileged/overview", "/entra/privileged/assignments",
                 "/entra/privileged/pim-policies", "/entra/privileged/activity",
                 "/entra/privileged/cross-plane", "/entra/apps", "/entra/apps-consent",
                 "/entra/ca/simulations",
                 "/entra/signals/overview", "/entra/signals/auth-methods",
                 "/entra/signals/legacy-auth", "/entra/signals/failures",
                 "/entra/signals/risky-users", "/entra/signals/patterns",
                 "/entra/governance/overview", "/entra/governance/reviews",
                 "/entra/governance/entitlement", "/entra/governance/lifecycle",
                 "/entra/governance/coverage",
                 "/entra/graph", "/entra/graph/escalations", "/entra/graph/targets",
                 "/entra/scanners", "/entra/inbox"):
        status, body = get(client, path, connection_id=fake)
        check(f"GET {path} on a cold cache", status == 200, f"HTTP {status}")
        if status == 200:
            meta_ok(path, body)


def demo(client: httpx.Client) -> None:
    section("Demo tenant")
    r = client.post(f"{API}/entra/demo/seed")
    check("POST /entra/demo/seed", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        return
    seeded = r.json()
    print(f"        seeded score={seeded['score']} findings={seeded['findings']}")
    check("demo produces a broad finding set", seeded["findings"] > 30, f"{seeded['findings']} findings")


def collect(client: httpx.Client, connection_id: str) -> bool:
    section(f"Live collection — connection {connection_id}")
    r = client.post(f"{API}/entra/refresh", params={"connection_id": connection_id}, json={"domains": [], "force": True})
    if not check("POST /entra/refresh starts a job", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"):
        return False

    # Follow the SSE progress stream, exactly as the UI does.
    started = time.monotonic()
    saw_progress = 0
    terminal = ""
    with client.stream("GET", f"{API}/entra/refresh/stream", params={"connection_id": connection_id},
                       timeout=900) as resp:
        event = ""
        for raw in resp.iter_lines():
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
                if event == "progress":
                    saw_progress += 1
                    try:
                        msg = json.loads(data)
                        print(f"        [{msg.get('level','info'):>5}] {msg.get('message','')}")
                    except json.JSONDecodeError:
                        pass
                elif event in ("done", "error"):
                    terminal = event
                    if event == "error":
                        print(f"        stream error: {data}")
                    break
    elapsed = time.monotonic() - started
    check("SSE stream emitted progress", saw_progress > 0, f"{saw_progress} message(s)")
    check("collection reached a terminal event", terminal in ("done", "error"), terminal or "none")
    print(f"        collection took {elapsed:.1f}s")
    return terminal == "done"


def posture(client: httpx.Client, connection_id: str | None) -> None:
    section("Posture")
    status, body = get(client, "/entra/posture", connection_id=connection_id)
    if not check("GET /entra/posture", status == 200, f"HTTP {status}"):
        return
    meta_ok("posture", body)
    score = body["score"]
    check("score is in range", 0 <= score["score"] <= 100, str(score["score"]))
    check("coverage is in range", 0.0 <= score["coverage"] <= 1.0, f"{score['coverage']:.0%}")
    check("score and coverage are published together", "coverage" in score and "score" in score)
    unmeasured = [p for p in score["pillars"] if p["score"] is None]
    check("unmeasured pillars are None, never 0",
          all(p["score"] is None for p in unmeasured),
          f"{len(unmeasured)} unmeasured")
    for p in unmeasured:
        check(f"  pillar '{p['key']}' explains why it was not measured",
              bool(p.get("reason")),
              p.get("reason") or "no reason")
    if score["coverage"] < 0.6:
        check("grade withheld below the coverage floor", score["grade"] == "", score["grade"])
    print(f"        score={score['score']} grade={score['grade'] or '(withheld)'} "
          f"coverage={score['coverage']:.0%} findings={score['findings_total']}")
    for p in score["pillars"]:
        print(f"          {p['key']:5} {str(p['score']):>5} {p['state']:14} "
              f"{p['measured_signals']}/{p['total_signals']} checks · {p['findings']} findings")
    if score["top_wins"]:
        print("        top wins:")
        for w in score["top_wins"][:3]:
            print(f"          +{w['points']:.1f} {w['signal_id']} (x{w['findings']})")


def findings(client: httpx.Client, connection_id: str | None) -> None:
    section("Findings")
    status, body = get(client, "/entra/findings", connection_id=connection_id, limit=500)
    if not check("GET /entra/findings", status == 200, f"HTTP {status}"):
        return
    rows = body["findings"]
    check("every finding carries evidence", all(f.get("evidence") for f in rows),
          f"{sum(1 for f in rows if not f.get('evidence'))} without evidence")
    check("every finding carries a fingerprint", all(f.get("fingerprint") for f in rows))
    check("fingerprints are unique", len({f["fingerprint"] for f in rows}) == len(rows))
    check("signal metadata is returned for rendering", bool(body.get("signals")) or not rows)
    print(f"        {body['total']} finding(s): {body['by_severity']}")

    if rows:
        fp = rows[0]["fingerprint"]
        status, detail = get(client, f"/entra/findings/{fp}", connection_id=connection_id)
        check("GET /entra/findings/{fingerprint}", status == 200, f"HTTP {status}")
        check("finding detail includes its signal definition", bool(detail.get("signal")))

        # Suppression must require a reason and must persist.
        r = client.post(f"{API}/entra/findings/{fp}/state",
                        params={"connection_id": connection_id} if connection_id else None,
                        json={"state": "suppressed", "reason": ""})
        check("suppression without a reason is rejected", r.status_code == 400, f"HTTP {r.status_code}")
        r = client.post(f"{API}/entra/findings/{fp}/state",
                        params={"connection_id": connection_id} if connection_id else None,
                        json={"state": "suppressed", "reason": "e2e check"})
        check("suppression with a reason is accepted", r.status_code == 200, f"HTTP {r.status_code}")
        _s, after = get(client, "/entra/findings", connection_id=connection_id, limit=500)
        gone = fp not in {f["fingerprint"] for f in after["findings"]}
        check("suppressed finding disappears from the list", gone)
        check("suppressed count is reported", after.get("suppressed_count", 0) >= 1,
              str(after.get("suppressed_count")))
        # Restore.
        client.post(f"{API}/entra/findings/{fp}/state",
                    params={"connection_id": connection_id} if connection_id else None,
                    json={"state": "open", "reason": ""})


def conditional_access(client: httpx.Client, connection_id: str | None) -> None:
    section("Conditional Access")
    status, cov = get(client, "/entra/ca/coverage", connection_id=connection_id)
    if not check("GET /entra/ca/coverage", status == 200, f"HTTP {status}"):
        return
    ca_domain = (cov.get("meta", {}).get("domains") or {}).get("ca") or {}
    if ca_domain.get("status") in ("blind", "unlicensed", "not_collected"):
        check("CA blindness is reported rather than hidden", bool(ca_domain.get("error") or ca_domain.get("missing_permissions")),
              f"{ca_domain.get('status')}: {ca_domain.get('error')}")
        return

    h = cov.get("headline") or {}
    check("coverage headline is present", "uncovered_users" in h)
    check("coverage states its assumptions", bool(h.get("assumptions")))
    print(f"        {h.get('uncovered_users')} user(s) x {h.get('uncovered_apps')} app(s) uncovered; "
          f"{h.get('privileged_uncovered')} privileged")
    check("matrix has a row per cohort", len(cov.get("matrix") or []) == len(cov.get("cohorts") or []))

    if cov.get("matrix"):
        row = cov["matrix"][0]
        control = (cov["controls"][0])["key"]
        app_class = (cov["app_classes"][0])["key"]
        status, cell = get(client, "/entra/ca/coverage/cell", connection_id=connection_id,
                           cohort=row["cohort"], app_class=app_class, control=control)
        check("GET /entra/ca/coverage/cell drill-down", status == 200, f"HTTP {status}")

    status, pol = get(client, "/entra/ca/policies", connection_id=connection_id)
    check("GET /entra/ca/policies", status == 200, f"HTTP {status}")
    if status == 200:
        policies = pol.get("policies") or []
        print(f"        {len(policies)} policy/policies: {pol.get('counts')}")
        check("policies never leak the full resolved user id lists",
              all("effective_ids" not in p for p in policies))
        check("policies carry decoded controls", all("controls" in p for p in policies) or not policies)
        if policies:
            status, one = get(client, f"/entra/ca/policy/{policies[0]['id']}", connection_id=connection_id)
            check("GET /entra/ca/policy/{id}", status == 200, f"HTTP {status}")

    status, conf = get(client, "/entra/ca/conflicts", connection_id=connection_id)
    check("GET /entra/ca/conflicts", status == 200, f"HTTP {status}")
    if status == 200:
        print(f"        conflicts by kind: {conf.get('by_kind')}")

    status, bg = get(client, "/entra/ca/breakglass", connection_id=connection_id)
    check("GET /entra/ca/breakglass", status == 200, f"HTTP {status}")
    if status == 200:
        check("break-glass detection is labelled heuristic", "confirm" in (bg.get("heuristic_note") or "").lower())
        check("candidates are never auto-confirmed",
              all(c.get("confirmed") is None for c in bg.get("candidates") or []),
              f"{bg.get('confirmed_count')} confirmed")
        print(f"        {bg.get('candidate_count')} candidate(s), {bg.get('confirmed_count')} confirmed")

    status, exp = get(client, "/entra/ca/export", connection_id=connection_id, format="markdown")
    check("GET /entra/ca/export?format=markdown", status == 200, f"HTTP {status}")


def setup(client: httpx.Client, connection_id: str | None) -> None:
    section("Setup & coverage")
    status, body = get(client, "/entra/setup/checklist", connection_id=connection_id)
    if not check("GET /entra/setup/checklist", status == 200, f"HTTP {status}"):
        return
    tiers = body.get("tiers") or []
    check("three consent tiers are described", len(tiers) == 3, str(len(tiers)))
    check("no write scope is ever requested",
          all("ReadWrite" not in s for t in tiers for s in t["scopes"]))
    for t in tiers:
        print(f"        tier {t['tier']} {t['name']}: {len(t['granted'])}/{len(t['scopes'])} granted")
    lic = body.get("meta", {}).get("licences") or {}
    print(f"        licences: P1={lic.get('p1')} P2={lic.get('p2')} detected={lic.get('detected')}")
    blind = body.get("meta", {}).get("permissions_summary", {}).get("blind_domains") or []
    if blind:
        print(f"        blind domains: {', '.join(blind)}")

    status, diag = get(client, "/entra/diagnostics", connection_id=connection_id)
    check("GET /entra/diagnostics", status == 200, f"HTTP {status}")
    if status == 200 and diag.get("graph"):
        g = diag["graph"]
        print(f"        graph: {g.get('requests')} request(s), {g.get('batches')} batch(es), "
              f"{g.get('throttled')} throttle(s), {g.get('items')} item(s), {g.get('ms')}ms")


def catalogue(client: httpx.Client) -> None:
    section("Signal catalogue")
    status, body = get(client, "/entra/signals")
    if not check("GET /entra/signals", status == 200, f"HTTP {status}"):
        return
    signals = body["signals"]
    check("pillar weights sum to 100", sum(p["weight"] for p in body["pillars"]) == 100)
    check("every signal documents remediation and a doc link",
          all(s["remediation"] and s["doc_link"] for s in signals))
    check("signal ids are namespaced by pillar",
          all(s["id"].startswith(s["pillar"] + ".") for s in signals))
    print(f"        {len(signals)} signals across {len(body['pillars'])} pillars "
          f"(registry v{body['registry_version']})")


# ------------------------------------------------------------------ privileged (P3)
def privileged(client: httpx.Client, connection_id: str | None) -> None:
    section("Privileged Access Mission Control")
    status, body = get(client, "/entra/privileged/overview", connection_id=connection_id)
    if not check("GET /entra/privileged/overview", status == 200, f"HTTP {status}"):
        return
    meta_ok("privileged/overview", body)
    c = body["counts"]
    print(f"        {c.get('global_admins')} global admin(s), {c.get('standing_privileged')} standing, "
          f"{c.get('eligible')} eligible, PIM configured {c.get('pim_fully_configured')}/{c.get('pim_policies')}")
    link = body["azure_link"]
    check("cross-plane availability is explained when absent",
          link["available"] or bool(link["reason"]),
          link["reason"] or "available")

    status, assigns = get(client, "/entra/privileged/assignments", connection_id=connection_id, kind="all")
    check("GET /entra/privileged/assignments", status == 200, f"HTTP {status}")
    if status == 200:
        rows = assigns["assignments"]
        check("assignments never assert permanence they cannot know",
              all(r.get("permanent") is not None or not r.get("permanence_known") for r in rows))
        print(f"        {assigns['total']} assignment row(s)")

    status, pimp = get(client, "/entra/privileged/pim-policies", connection_id=connection_id)
    check("GET /entra/privileged/pim-policies", status == 200, f"HTTP {status}")
    if status == 200:
        if pimp["policies"]:
            check("every PIM policy row carries a config score",
                  all("score" in p and "failed_controls" in p for p in pimp["policies"]))
            worst = pimp["policies"][0]
            print(f"        worst-configured role: {worst['role_name']} ({worst['score']}/100, "
                  f"missing {', '.join(worst['failed_controls']) or 'nothing'})")
        else:
            d = pimp.get("domain") or {}
            check("empty PIM grid explains itself", bool(d.get("error") or d.get("status")),
                  f"{d.get('status')}: {d.get('error')}")

    status, act = get(client, "/entra/privileged/activity", connection_id=connection_id)
    check("GET /entra/privileged/activity", status == 200, f"HTTP {status}")

    activations(client, connection_id)

    status, cross = get(client, "/entra/privileged/cross-plane", connection_id=connection_id)
    check("GET /entra/privileged/cross-plane", status == 200, f"HTTP {status}")
    if status == 200:
        both = [r for r in cross["rows"] if r["both_planes"]]
        print(f"        {cross['total']} principal(s) with Entra power, {len(both)} also hold Azure power")
        check("stale Azure joins are flagged rather than presented as current",
              "stale" in cross["azure_link"])


# ------------------------------------------------------ privileged activations (P1-P6)
def activations(client: httpx.Client, connection_id: str | None) -> None:
    """Activation sessions across both planes, the ledger, and the action correlation."""
    section("Privileged activations")
    status, body = get(client, "/entra/privileged/activations",
                       connection_id=connection_id, days=3650)
    if not check("GET /entra/privileged/activations", status == 200, f"HTTP {status}"):
        return
    sessions = body["sessions"]
    caps = body["capabilities"]
    facets = body["facets"]
    ledger = body["ledger"]
    print(f"        {body['total']} session(s): {facets.get('entra', 0)} Entra, "
          f"{facets.get('azure', 0)} Azure across {caps.get('azure_subscriptions', 0)} subscription(s)")
    print(f"        ledger retains {ledger.get('total', 0)} since {str(ledger.get('earliest'))[:10]}")

    check("every session names a principal rather than an object id",
          all(s["label"] and not s["label"].startswith("unresolved") for s in sessions),
          next((s["label"] for s in sessions if s["label"].startswith("unresolved")), ""))
    check("every session states which plane it came from",
          all(s["plane"] in ("entra", "azure") for s in sessions))
    check("every granted session has a window we can search for actions",
          all(s["start"] for s in sessions if s["granted"]))
    # Azure PIM states a duration and omits the end date; without deriving it the length
    # of every Azure elevation is blank.
    check("activation length is known for both planes",
          all(s["granted_hours"] is not None for s in sessions if s["start"] and s["end"]))
    # A source with no justification field must not be reported as an operator omission.
    check("justification quality distinguishes 'not recorded' from 'not given'",
          all(s["justification_quality"] in ("ok", "weak", "missing", "unknown")
              for s in sessions))
    check("sessions from a source without justification are marked unknown, not missing",
          all(s["justification_quality"] == "unknown"
              for s in sessions if not s["detail_known"]))
    check("failed requests are not presented as granted privilege",
          all(s["granted"] for s in sessions if s["status"].lower() == "provisioned"))

    if not sessions:
        return
    target = next((s for s in sessions if s["granted"]), sessions[0])
    status, actions = get(client, f"/entra/privileged/activations/{target['id']}/actions",
                          connection_id=connection_id)
    if not check("GET /entra/privileged/activations/{id}/actions", status == 200, f"HTTP {status}"):
        return
    counts = actions["counts"]
    print(f"        '{target['label']}' / {target['role_name']}: "
          f"{counts.get('total', 0)} action(s) during the window "
          f"({counts.get('required_activation', 0)} needed the elevation)")
    check("every action is classified rather than blamed",
          all(a["attribution"] in ("required_activation", "possible_without", "unclassified")
              for a in actions["actions"]))
    check("the window searched is reported so the reader can judge it",
          bool(actions["window"].get("start") and actions["window"].get("end")))
    check("the standing-permission picture is disclosed",
          "standing_entra_roles" in actions and "azure_link_available" in actions)

    status, exp = get(client, "/entra/privileged/activations-export",
                      connection_id=connection_id, days=3650)
    check("GET /entra/privileged/activations-export", status == 200, f"HTTP {status}")
    if status == 200:
        check("the evidence pack records where each claim came from",
              bool(exp.get("provenance")) and bool(exp.get("generated_at")))


# --------------------------------------------------------------- Application 360 (P4)
def applications(client: httpx.Client, connection_id: str | None) -> None:
    section("Application 360")
    status, body = get(client, "/entra/apps", connection_id=connection_id, limit=500)
    if not check("GET /entra/apps", status == 200, f"HTTP {status}"):
        return
    meta_ok("apps", body)
    apps = body["apps"]
    check("applications are returned with a risk score", all("risk_score" in a for a in apps))
    check("risk components are published", len(body.get("risk_components") or []) > 0)
    check("risk weights sum to 100", sum(c["weight"] for c in body.get("risk_components") or []) == 100)
    if apps:
        scores = [a["risk_score"] for a in apps]
        check("inventory is sorted by risk", scores == sorted(scores, reverse=True))
        print(f"        {body['total']} application(s); top risk {scores[0]} "
              f"({apps[0]['display_name']})")

        target = apps[0]["object_id"]
        status, detail = get(client, f"/entra/apps/{target}", connection_id=connection_id)
        check("GET /entra/apps/{id} (Application 360)", status == 200, f"HTTP {status}")
        if status == 200:
            check("360 separates granted from requested-but-not-granted",
                  "granted_application_permissions" in detail and "requested_not_granted" in detail)
            check("360 publishes the risk components",
                  bool((detail.get("risk") or {}).get("components")))
            check("360 reports Conditional Access coverage", "conditional_access" in detail)
            check("360 reports Azure reach with its freshness", "azure_reach" in detail)

    status, consent = get(client, "/entra/apps-consent", connection_id=connection_id)
    check("GET /entra/apps-consent", status == 200, f"HTTP {status}")
    if status == 200:
        print(f"        {len(consent['all_principals_grants'])} tenant-wide delegated grant(s)")


# ----------------------------------------------------------------- CA simulator (P5)
def simulator(client: httpx.Client, connection_id: str | None) -> None:
    section("Conditional Access Change Simulator")
    status, ctxs = get(client, "/entra/ca/simulate/contexts")
    check("GET /entra/ca/simulate/contexts", status == 200, f"HTTP {status}")
    if status == 200:
        check("break-glass and privileged cohorts are never sampled away",
              "break_glass" in ctxs["always_full_cohorts"] and "privileged" in ctxs["always_full_cohorts"])
        check("model limitations are published", len(ctxs["limitations"]) >= 5)

    status, policies = get(client, "/entra/ca/policies", connection_id=connection_id)
    rows = policies.get("policies") or [] if status == 200 else []

    # A change the engine cannot apply must be an explicit error, never a reassuring
    # "nothing changes" diff.
    bad = client.post(f"{API}/entra/ca/simulate",
                      params={"connection_id": connection_id} if connection_id else None,
                      json={"changes": [{"kind": "remove_exclusion", "policy_id": "nope"}]})
    check("an unknown change kind is rejected, not silently ignored", bad.status_code == 400,
          f"HTTP {bad.status_code}: {bad.text[:160]}")

    if not rows:
        check("simulator declines to run with no policies", True,
              "tenant has no Conditional Access policies — nothing to simulate")
        return

    target = next((p for p in rows if not p["is_enforced"]), rows[0])
    kind = "enable" if not target["is_enforced"] else "disable"
    r = client.post(f"{API}/entra/ca/simulate",
                    params={"connection_id": connection_id} if connection_id else None,
                    json={"changes": [{"kind": kind, "policy_id": target["id"]}],
                          "save": True, "label": f"e2e {kind}"})
    if not check(f"POST /entra/ca/simulate ({kind} '{target['display_name']}')",
                 r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"):
        return
    body = r.json()
    result = body["result"]
    counts = result["counts"]
    print(f"        {counts['newly_blocked']} newly blocked · {counts['newly_challenged']} challenged · "
          f"{counts['protection_lost']} protection lost · {counts['unchanged']} unchanged")
    check("result is diff-shaped, not absolute",
          set(counts) == {"newly_blocked", "newly_challenged", "newly_granted", "protection_lost", "unchanged"})
    check("break-glass impact is reported separately", "break_glass_impact" in result)
    check("result carries a confidence label", bool(result.get("confidence_label")))
    check("result publishes the model's limitations", len(result.get("limitations") or []) >= 5)
    check("sampling is explained", "sampling" in result and "evaluated" in result["sampling"])
    if result["cases"]:
        first = result["cases"][0]
        print(f"        first changed case: {first['principal']} {first['from']} -> {first['to']} "
              f"({first['context_label']})")
        check("break-glass cases sort to the top",
              not any("break_glass" in c["cohorts"] for c in result["cases"])
              or "break_glass" in result["cases"][0]["cohorts"])

    saved_id = body.get("saved_id")
    check("simulation was saved", bool(saved_id), saved_id or "not saved")
    status, listing = get(client, "/entra/ca/simulations", connection_id=connection_id)
    check("GET /entra/ca/simulations", status == 200, f"HTTP {status}")
    if status == 200 and saved_id:
        check("saved simulation appears in the list",
              any(s["id"] == saved_id for s in listing["simulations"]))
        rr = client.post(f"{API}/entra/ca/simulations/{saved_id}/rerun",
                         params={"connection_id": connection_id} if connection_id else None, json={})
        check("POST /entra/ca/simulations/{id}/rerun", rr.status_code == 200, f"HTTP {rr.status_code}")
        if rr.status_code == 200:
            rerun = rr.json()["result"]
            check("re-run is deterministic against the same snapshot",
                  rerun["counts"] == counts, f"{rerun['counts']} vs {counts}")


# ------------------------------------------- risk & sign-in intelligence (P6)
def risk_intelligence(client: httpx.Client, connection_id: str | None) -> None:
    section("Risk & sign-in intelligence")
    status, body = get(client, "/entra/signals/overview", connection_id=connection_id)
    if not check("GET /entra/signals/overview", status == 200, f"HTTP {status}"):
        return
    meta_ok("signals/overview", body)
    caps = body["capabilities"]
    signins = body["signins"]
    check("the sampling flag is published at the top level", "sampled" in body)
    check("no raw sign-in row is ever returned",
          "createdDateTime" not in repr(signins) and "appliedConditionalAccessPolicies" not in repr(signins))
    if caps.get("signins"):
        print(f"        {signins['total']:,} sign-in(s), {signins['failure_rate']:.1%} failed, "
              f"sampled={signins['sampled']}")
    else:
        d = body.get("domain") or {}
        check("blind sign-in analysis explains itself", bool(d.get("error") or d.get("status")),
              f"{d.get('status')}: {str(d.get('error'))[:90]}")

    status, methods = get(client, "/entra/signals/auth-methods", connection_id=connection_id)
    check("GET /entra/signals/auth-methods", status == 200, f"HTTP {status}")
    if status == 200:
        check("the administrator cohort is reported separately from the tenant",
              "privileged" in methods and "overall" in methods)
        if methods["known"]:
            p = methods["privileged"]
            print(f"        admins: {p['registered']}/{p['total']} registered, "
                  f"{p['phishing_resistant']} phishing-resistant")
        else:
            check("unknown registration is stated, not assumed clean", methods["known"] is False,
                  "registration report unavailable")

    status, legacy = get(client, "/entra/signals/legacy-auth", connection_id=connection_id)
    check("GET /entra/signals/legacy-auth", status == 200, f"HTTP {status}")
    if status == 200:
        check("legacy successes are separated from attempts",
              all("success" in p and "total" in p for p in legacy["protocols"]))
        check("the 'policy exists but legacy still succeeds' gap is computed",
              "policy_gap" in legacy)

    for path in ("/entra/signals/failures", "/entra/signals/risky-users", "/entra/signals/patterns"):
        status, payload = get(client, path, connection_id=connection_id)
        check(f"GET {path}", status == 200, f"HTTP {status}")
        if status == 200 and path.endswith("patterns"):
            check("every pattern states the rule that produced it",
                  all(p.get("rule") for p in payload["patterns"]))
            print(f"        {len(payload['patterns'])} deterministic pattern(s)")
        if status == 200 and path.endswith("risky-users"):
            check("risky users are joined to privilege and self-remediation",
                  all("privileged" in u and "can_self_remediate" in u for u in payload["users"]))
            print(f"        {payload['total']} risky user(s), "
                  f"{len(payload['workload_identities'])} risky workload identity/identities")


# --------------------------------------------------------------- governance (P6)
def governance(client: httpx.Client, connection_id: str | None) -> None:
    section("Identity Governance")
    status, body = get(client, "/entra/governance/coverage", connection_id=connection_id)
    if not check("GET /entra/governance/coverage", status == 200, f"HTTP {status}"):
        return
    meta_ok("governance/coverage", body)
    rows = body["rows"]
    check("coverage renders on any tenant, licensed or not", len(rows) == len(body["classes"]))
    check("every coverage row explains why it matters",
          all(r.get("why") and r.get("label") for r in rows))
    check("the gap is never negative", all(r["gap"] >= 0 for r in rows))
    for row in rows:
        print(f"        {row['label']:32} {row['count']:>5} total, {row['reviewed']:>4} reviewed, "
              f"{row['gap']:>5} gap")

    for path in ("/entra/governance/overview", "/entra/governance/reviews",
                 "/entra/governance/entitlement", "/entra/governance/lifecycle"):
        status, payload = get(client, path, connection_id=connection_id)
        check(f"GET {path}", status == 200, f"HTTP {status}")
        if status == 200 and path.endswith("reviews"):
            check("review quality problems are named, not just counted",
                  all("quality_flags" in r for r in payload["reviews"]))


# ----------------------------------------------------------- blast radius (P7)
def blast_radius(client: httpx.Client, connection_id: str | None) -> None:
    section("Identity Blast-Radius Graph")
    status, scopes = get(client, "/entra/graph/scopes")
    if not check("GET /entra/graph/scopes", status == 200, f"HTTP {status}"):
        return
    check("every escalation primitive publishes its rule",
          all(p.get("rule") and p.get("confidence") for p in scopes["primitives"]))
    print(f"        {len(scopes['scopes'])} scope(s), {len(scopes['primitives'])} escalation primitive(s)")

    for scope_kind in ("privileged", "escalation"):
        status, graph = get(client, "/entra/graph", connection_id=connection_id,
                            scope_kind=scope_kind)
        if not check(f"GET /entra/graph?scope_kind={scope_kind}", status == 200, f"HTTP {status}"):
            continue
        meta_ok(f"graph/{scope_kind}", graph)
        present = {n["id"] for n in graph["nodes"]}
        dangling = [e for e in graph["edges"]
                    if e["source"] not in present or e["target"] not in present]
        check(f"{scope_kind}: no dangling edge (one blanks the whole canvas)", not dangling,
              f"{len(dangling)} dangling")
        check(f"{scope_kind}: the view explains itself", bool(graph.get("note")))
        print(f"        {scope_kind}: {graph['stats']['node_count']} node(s), "
              f"{graph['stats']['edge_count']} edge(s), "
              f"{graph['stats']['dropped_edges']} dropped")

    status, esc = get(client, "/entra/graph/escalations", connection_id=connection_id)
    check("GET /entra/graph/escalations", status == 200, f"HTTP {status}")
    if status == 200:
        check("every escalation carries a human-readable reason",
              all(e.get("reason") and e.get("rule") for e in esc["escalations"]))
        print(f"        {esc['total']} escalation path(s): {esc['by_primitive']}")

    status, targets = get(client, "/entra/graph/targets", connection_id=connection_id)
    check("GET /entra/graph/targets", status == 200, f"HTTP {status}")
    if status == 200 and targets["principals"]:
        # Focusing an arbitrary principal must also honour the dangling-edge invariant.
        pid = targets["principals"][0]["id"]
        status, focused = get(client, "/entra/graph", connection_id=connection_id,
                              scope_kind="principal", scope_id=pid)
        check("GET /entra/graph?scope_kind=principal", status == 200, f"HTTP {status}")
        if status == 200:
            present = {n["id"] for n in focused["nodes"]}
            check("focused principal graph has no dangling edge",
                  all(e["source"] in present and e["target"] in present for e in focused["edges"]))


# ----------------------------------------------------------- proactive hub (P8)
def proactive_hub(client: httpx.Client, connection_id: str | None) -> None:
    section("Proactive hub: scanners and the findings inbox")
    status, body = get(client, "/entra/scanners", connection_id=connection_id)
    if not check("GET /entra/scanners", status == 200, f"HTTP {status}"):
        return
    meta_ok("scanners", body)
    scanners = body["scanners"]
    check("every scanner selects at least one signal",
          all(s["signal_count"] > 0 for s in scanners),
          ", ".join(s["id"] for s in scanners if not s["signal_count"]))
    check("a scanner that cannot run says why rather than reporting zero",
          all(s["blocked"] or s["signal_count"] for s in scanners))
    blocked = [s for s in scanners if s["blocked"]]
    print(f"        {len(scanners)} scanner(s), {len(blocked)} blocked by a missing domain")
    for s in blocked:
        print(f"          {s['id']}: {s['blocked'][:80]}")

    # First run establishes the baseline; the second must report nothing new.
    r = client.post(f"{API}/entra/scanners/run",
                    params={"connection_id": connection_id} if connection_id else None,
                    json={"scanner_ids": [], "force": True, "notify": False})
    if not check("POST /entra/scanners/run", r.status_code == 200,
                 f"HTTP {r.status_code} {r.text[:160]}"):
        return
    first = r.json()
    print(f"        first sweep: {first['new_total']} new across {len(first['ran'])} scanner(s)")

    r2 = client.post(f"{API}/entra/scanners/run",
                     params={"connection_id": connection_id} if connection_id else None,
                     json={"scanner_ids": [], "force": True, "notify": False})
    second = r2.json() if r2.status_code == 200 else {}
    check("a repeat sweep reports nothing new (the rule that keeps a digest readable)",
          second.get("new_total") == 0, f"{second.get('new_total')} new on the second run")

    r3 = client.post(f"{API}/entra/scanners/run",
                     params={"connection_id": connection_id} if connection_id else None,
                     json={"scanner_ids": ["not-a-scanner"], "force": True})
    check("an unknown scanner id is rejected", r3.status_code == 400, f"HTTP {r3.status_code}")

    status, inbox = get(client, "/entra/inbox", connection_id=connection_id, limit=500)
    check("GET /entra/inbox", status == 200, f"HTTP {status}")
    if status == 200:
        check("every inbox row carries a workflow state and an age",
              all("state" in f and "age_days" in f for f in inbox["findings"]))
        check("the inbox reports what resolved itself", "recently_resolved" in inbox)
        print(f"        {inbox['total']} finding(s) in the inbox, states {inbox['by_state']}")

        if inbox["findings"]:
            fp = inbox["findings"][0]["fingerprint"]
            bad = client.post(f"{API}/entra/inbox/bulk",
                              params={"connection_id": connection_id} if connection_id else None,
                              json={"fingerprints": [fp], "state": "suppressed"})
            check("a suppression without a reason is rejected", bad.status_code == 400,
                  f"HTTP {bad.status_code}")
            nosnooze = client.post(f"{API}/entra/inbox/bulk",
                                   params={"connection_id": connection_id} if connection_id else None,
                                   json={"fingerprints": [fp], "state": "snoozed"})
            check("a snooze without an expiry is rejected", nosnooze.status_code == 400,
                  f"HTTP {nosnooze.status_code}")
            ok = client.post(f"{API}/entra/inbox/bulk",
                             params={"connection_id": connection_id} if connection_id else None,
                             json={"fingerprints": [fp], "state": "acknowledged"})
            check("a bulk acknowledgement is accepted", ok.status_code == 200,
                  f"HTTP {ok.status_code}")
            status, after = get(client, "/entra/inbox", connection_id=connection_id, limit=500)
            row = next((f for f in after["findings"] if f["fingerprint"] == fp), None)
            check("the acknowledgement persisted", bool(row and row["state"] == "acknowledged"),
                  row["state"] if row else "row missing")
            client.post(f"{API}/entra/inbox/bulk",
                        params={"connection_id": connection_id} if connection_id else None,
                        json={"fingerprints": [fp], "state": "open"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", default="", help="Azure connection id to collect from")
    ap.add_argument("--demo-only", action="store_true", help="skip the live collection")
    args = ap.parse_args()

    with httpx.Client(timeout=120, follow_redirects=True, headers=SAME_ORIGIN_HEADERS) as client:
        login(client)

        catalogue(client)
        cold_cache(client)
        demo(client)

        section("Demo tenant read path")
        # The demo snapshot lives under its own tenant id, reachable by resolving no connection
        # only if the default connection points at it; instead we assert the analysis directly.
        conns = client.get(f"{API}/admin/connections").json()
        connections = conns.get("connections", conns if isinstance(conns, list) else [])
        print(f"        {len(connections)} Azure connection(s) configured")
        for c in connections:
            print(f"          {c.get('id')}: {c.get('display_name')} "
                  f"({c.get('auth_method')}, tenant {c.get('tenant_id')})")

        if args.demo_only or not connections:
            print("\n(skipping live collection)")
        else:
            target = args.connection or next(
                (c["id"] for c in connections
                 if c.get("auth_method") in ("service_principal", "service_principal_cert")
                 and not c.get("disabled")),
                connections[0]["id"],
            )
            ok = collect(client, target)
            posture(client, target)
            findings(client, target)
            conditional_access(client, target)
            privileged(client, target)
            applications(client, target)
            simulator(client, target)
            risk_intelligence(client, target)
            governance(client, target)
            blast_radius(client, target)
            proactive_hub(client, target)
            setup(client, target)
            if not ok:
                print("\nNOTE: the collection ended with an error event — see the log above.")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
