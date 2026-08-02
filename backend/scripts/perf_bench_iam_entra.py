"""Wall-clock benchmark of every /iam and /entra read endpoint against a real tenant.

Why a script and not a test: the thing under measurement is cost at REAL tenant size, which no
fixture reproduces. The reference tenant here has 5,506 grants across 45 scopes; customers run
roughly twice that, so anything that is linear in grants should be read as double, and anything
quadratic as four times.

Every endpoint is called twice. The gap between the two is the whole point:

* slow cold, fast warm  -> a cache is doing its job; only the first visitor pays.
* slow cold, slow warm  -> the work is repeated on every page load. THIS is what to fix.
* fast cold             -> already fine, leave it alone.

Run against the local API on :8000 (start it WITHOUT --reload):

    .venv\\Scripts\\python.exe scripts\\perf_bench_iam_entra.py [--json out.json]

Always benchmark over 127.0.0.1, never "localhost": on Windows the latter resolves to ::1
first, fails, and falls back to IPv4, adding a flat ~2s per request that swamps the signal.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

# Endpoints that stream, mutate, or block on a live Azure call. Excluded because their cost is
# network-bound rather than compute-bound: they would drown out the signal we are after.
SKIP = {"/api/iam/refresh/stream", "/api/entra/refresh/stream", "/api/iam/job"}


def call(method: str, path: str, body: dict | None = None, timeout: int = 600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if data:
        req.add_header("Content-Type", "application/json")
    if method not in ("GET", "HEAD"):
        req.add_header("Sec-Fetch-Site", "same-origin")
    started = time.monotonic()
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw, (time.monotonic() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300], (time.monotonic() - started) * 1000
    except Exception as exc:  # noqa: BLE001 - a benchmark must survive a broken endpoint
        return 0, str(exc)[:300].encode(), (time.monotonic() - started) * 1000


def get_json(path: str):
    st, raw, _ = call("GET", path)
    if st != 200:
        return None
    try:
        return json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        return None


def openapi_paths(prefix: str) -> list[str]:
    spec = json.load(urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=60))
    return sorted(
        p for p in spec["paths"]
        if p.startswith(prefix) and "get" in spec["paths"][p] and p not in SKIP
    )


def _first(seq, *keys):
    """First usable id out of a list response, tolerating the various envelope shapes."""
    for item in seq or []:
        if not isinstance(item, dict):
            continue
        for k in keys:
            if item.get(k):
                return item[k]
    return None


def resolve_params(cid: str) -> dict[str, str]:
    """Fill {path_param} placeholders from real data so those routes are measured, not skipped."""
    q = f"?connection_id={cid}"
    out: dict[str, str] = {}

    scanners = get_json(f"/iam/scanners{q}") or {}
    out["scanner_id"] = _first(scanners.get("scanners") or scanners.get("items"), "id", "key") or ""

    roles = get_json(f"/iam/access{q}&limit=1") or {}
    rows = roles.get("rows") or roles.get("items") or []
    out["principal_id"] = _first(rows, "principalId", "principal_id") or ""

    runs = get_json(f"/iam/runs{q}") or {}
    out["run_id"] = _first(runs.get("runs") or runs.get("items"), "id", "run_id") or ""

    camps = get_json(f"/iam/campaigns{q}") or {}
    out["campaign_id"] = _first(camps.get("campaigns") or camps.get("items"), "id") or ""

    apps = get_json(f"/entra/apps{q}") or {}
    out["object_id"] = _first(apps.get("apps") or apps.get("items"), "objectId", "object_id", "id") or ""

    pol = get_json(f"/entra/ca/policies{q}") or {}
    out["policy_id"] = _first(pol.get("policies") or pol.get("items"), "id", "policyId") or ""

    fnd = get_json(f"/entra/findings{q}") or {}
    out["fingerprint"] = _first(fnd.get("findings") or fnd.get("items"), "fingerprint") or ""

    sims = get_json(f"/entra/ca/simulations{q}") or {}
    out["simulation_id"] = _first(sims.get("simulations") or sims.get("items"), "id") or ""

    acts = get_json(f"/entra/privileged/activations{q}") or {}
    out["session_id"] = _first(acts.get("activations") or acts.get("items"), "sessionId", "id") or ""

    priv = get_json(f"/entra/privileged/assignments{q}") or {}
    out["pillar"] = "identity"
    return out


def bench(prefix: str, cid: str, label: str) -> list[dict]:
    params = resolve_params(cid)
    results = []
    for path in openapi_paths(prefix):
        route = path[len("/api"):]
        skipped = False
        for name, value in params.items():
            token = "{" + name + "}"
            if token in route:
                if not value:
                    skipped = True
                route = route.replace(token, str(value))
        if skipped or "{" in route:
            results.append({"path": path, "status": "no-data", "cold": None, "warm": None, "bytes": 0})
            continue
        sep = "&" if "?" in route else "?"
        url = f"{route}{sep}connection_id={cid}"
        st1, raw1, cold = call("GET", url)
        st2, _, warm = call("GET", url)
        results.append({
            "path": path, "status": st1, "cold": round(cold), "warm": round(warm),
            "bytes": len(raw1 or b""),
        })
        print(f"  {cold:8.0f} {warm:8.0f} ms  {len(raw1 or b''):>9,}B  {st1}  {path}", flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the raw numbers here for before/after comparison")
    ap.add_argument("--iam-connection", default=None)
    ap.add_argument("--entra-connection", default=None)
    args = ap.parse_args()

    st, _, _ = call("POST", "/auth/login", {"username": "admin", "password": "admin"})
    if st != 200:
        print(f"login failed: {st}")
        return 1

    conns = (get_json("/azure/connections") or {}).get("connections", [])
    by_name = {c["display_name"]: c["id"] for c in conns}
    iam_cid = args.iam_connection or by_name.get("lu") or (conns[0]["id"] if conns else "")
    entra_cid = args.entra_connection or iam_cid

    print(f"connections: {', '.join(f'{c['display_name']}={c['status']}' for c in conns)}")
    print(f"iam connection   = {iam_cid}")
    print(f"entra connection = {entra_cid}\n")

    print("      cold     warm       payload  status  endpoint")
    print("=== /iam ===")
    iam = bench("/api/iam", iam_cid, "iam")
    print("=== /entra ===")
    entra = bench("/api/entra", entra_cid, "entra")

    every = iam + entra
    timed = [r for r in every if isinstance(r["cold"], int)]
    print("\n--- slowest WARM (repeated on every page load) ---")
    for r in sorted(timed, key=lambda r: -(r["warm"] or 0))[:15]:
        print(f"  {r['warm']:8,} ms  {r['path']}")
    print("\n--- largest payloads ---")
    for r in sorted(timed, key=lambda r: -r["bytes"])[:10]:
        print(f"  {r['bytes']:12,} B  {r['path']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(every, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
