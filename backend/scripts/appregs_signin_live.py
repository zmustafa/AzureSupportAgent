"""Live check: refresh App Registrations against a real connection and report what the new
sign-in columns actually contain. Dev helper — read-only apart from the refresh itself."""
import json
import sys

import httpx

API = "http://127.0.0.1:35001/api"
C = httpx.Client(timeout=1800, headers={"Origin": "http://127.0.0.1:35001",
                                        "Sec-Fetch-Site": "same-origin"})
C.post(f"{API}/auth/login", json={"username": "admin", "password": "admin"})

conns = C.get(f"{API}/admin/connections").json()
rows = conns if isinstance(conns, list) else conns.get("connections") or conns.get("items") or []
print("connections:")
for c in rows:
    print(f"  {c.get('id')}  {c.get('name')!r}  default={c.get('is_default')}  tenant={c.get('tenant_id')}")

cid = sys.argv[1] if len(sys.argv) > 1 else next(
    (c["id"] for c in rows if c.get("is_default")), rows[0]["id"] if rows else ""
)
print(f"\nrefreshing connection {cid} …")
r = C.post(f"{API}/identity/app-registrations/refresh", params={"connection_id": cid, "mode": "capped"})
print("HTTP", r.status_code)
snap = r.json()
if r.status_code != 200:
    print(json.dumps(snap)[:1500])
    raise SystemExit(1)

print("source:", snap.get("source"), "| note:", (snap.get("note") or "")[:160])
print("signin_activity:", json.dumps(snap.get("signin_activity"), indent=2))
s = snap.get("summary") or {}
print("summary:", {k: s.get(k) for k in
                   ("total", "signedIn7d", "signedIn30d", "noRecentSignIn", "signInNotMeasured")})
print("facet:", snap.get("facets", {}).get("signInActivity"))

apps = snap.get("apps") or []
with_dates = [a for a in apps if a.get("lastSignIn")]
print(f"\napps with a date: {len(with_dates)} of {len(apps)}")
for a in sorted(with_dates, key=lambda a: a["lastSignIn"], reverse=True)[:8]:
    print(f"  {a['displayName'][:44]:44} {a['lastSignIn']}  {a['lastSignInDays']}d"
          f"  app={bool(a['lastSignInApplication'])} del={bool(a['lastSignInDelegated'])}")

used = [(a['displayName'], c) for a in apps for c in a['credentials'] if c.get("lastUsed")]
print(f"\ncredentials with a last-use: {len(used)}")
for name, c in used[:6]:
    print(f"  {name[:36]:36} {c['type']:11} {c['displayName'][:20]:20} {c['lastUsed']}")
